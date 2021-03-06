# Copyright 2013 Cloudbase Solutions Srl
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import platform
import sys

if sys.platform == 'win32':
    import wmi

from oslo_log import log as logging

from os_win._i18n import _, _LE
from os_win import exceptions
from os_win.utils import _wqlutils
from os_win.utils import baseutils
from os_win.utils.compute import vmutils
from os_win.utils import jobutils
from os_win.utils.storage.initiator import iscsi_wmi_utils

LOG = logging.getLogger(__name__)


class LiveMigrationUtils(baseutils.BaseUtilsVirt):
    _STORAGE_ALLOC_SETTING_DATA_CLASS = 'Msvm_StorageAllocationSettingData'
    _CIM_RES_ALLOC_SETTING_DATA_CLASS = 'CIM_ResourceAllocationSettingData'

    def __init__(self):
        super(LiveMigrationUtils, self).__init__()
        self._vmutils = vmutils.VMUtils()
        self._jobutils = jobutils.JobUtils()
        self._iscsi_initiator = iscsi_wmi_utils.ISCSIInitiatorWMIUtils()

    def _get_conn_v2(self, host='localhost'):
        try:
            return self._get_wmi_obj(self._wmi_namespace % host)
        except wmi.x_wmi as ex:
            LOG.exception(_LE('Get version 2 connection error'))
            if ex.com_error.hresult == -2147217394:
                msg = (_('Live migration is not supported on target host "%s"')
                       % host)
            elif ex.com_error.hresult == -2147023174:
                msg = (_('Target live migration host "%s" is unreachable')
                       % host)
            else:
                msg = _('Live migration failed: %s') % ex.message
            raise exceptions.HyperVException(msg)

    def check_live_migration_config(self):
        migration_svc = (
            self._compat_conn.Msvm_VirtualSystemMigrationService()[0])
        vsmssd = (
            self._compat_conn.Msvm_VirtualSystemMigrationServiceSettingData())
        vsmssd = vsmssd[0]
        if not vsmssd.EnableVirtualSystemMigration:
            raise exceptions.HyperVException(
                _('Live migration is not enabled on this host'))
        if not migration_svc.MigrationServiceListenerIPAddressList:
            raise exceptions.HyperVException(
                _('Live migration networks are not configured on this host'))

    def _get_vm(self, conn_v2, vm_name):
        vms = conn_v2.Msvm_ComputerSystem(ElementName=vm_name)
        n = len(vms)
        if not n:
            raise exceptions.HyperVVMNotFoundException(vm_name=vm_name)
        elif n > 1:
            raise exceptions.HyperVException(_('Duplicate VM name found: %s')
                                             % vm_name)
        return vms[0]

    def _destroy_planned_vm(self, conn_v2, planned_vm):
        LOG.debug("Destroying existing planned VM: %s",
                  planned_vm.ElementName)
        vs_man_svc = conn_v2.Msvm_VirtualSystemManagementService()[0]
        (job_path, ret_val) = vs_man_svc.DestroySystem(planned_vm.path_())
        self._jobutils.check_ret_val(ret_val, job_path)

    def _get_planned_vms(self, conn_v2, vm):
        return conn_v2.Msvm_PlannedComputerSystem(Name=vm.Name)

    def _destroy_existing_planned_vms(self, conn_v2, vm):
        planned_vms = self._get_planned_vms(conn_v2, vm)
        for planned_vm in planned_vms:
            self._destroy_planned_vm(conn_v2, planned_vm)

    def _create_planned_vm(self, conn_v2_local, conn_v2_remote,
                           vm, ip_addr_list, dest_host):
        # Staged
        vsmsd = conn_v2_remote.query("select * from "
                                     "Msvm_VirtualSystemMigrationSettingData "
                                     "where MigrationType = 32770")[0]
        vsmsd.DestinationIPAddressList = ip_addr_list
        migration_setting_data = vsmsd.GetText_(1)

        LOG.debug("Creating planned VM for VM: %s", vm.ElementName)
        migr_svc = conn_v2_remote.Msvm_VirtualSystemMigrationService()[0]
        (job_path, ret_val) = migr_svc.MigrateVirtualSystemToHost(
            ComputerSystem=vm.path_(),
            DestinationHost=dest_host,
            MigrationSettingData=migration_setting_data)
        self._jobutils.check_ret_val(ret_val, job_path)

        return conn_v2_local.Msvm_PlannedComputerSystem(Name=vm.Name)[0]

    def _get_physical_disk_paths(self, vm_name):
        # TODO(claudiub): Remove this after the livemigrationutils usage has
        # been updated to create planned VM on the destination host beforehand.
        ide_ctrl_path = self._vmutils.get_vm_ide_controller(vm_name, 0)
        if ide_ctrl_path:
            ide_paths = self._vmutils.get_controller_volume_paths(
                ide_ctrl_path)
        else:
            ide_paths = {}

        scsi_ctrl_path = self._vmutils.get_vm_scsi_controller(vm_name)
        scsi_paths = self._vmutils.get_controller_volume_paths(scsi_ctrl_path)

        return dict(list(ide_paths.items()) + list(scsi_paths.items()))

    def _get_remote_disk_data(self, vmutils_remote, disk_paths, dest_host):
        # TODO(claudiub): Remove this after the livemigrationutils usage has
        # been updated to create planned VM on the destination host beforehand.
        remote_iscsi_initiator = iscsi_wmi_utils.ISCSIInitiatorWMIUtils(
            dest_host)

        disk_paths_remote = {}
        for (rasd_rel_path, disk_path) in disk_paths.items():
            target = self._iscsi_initiator.get_target_from_disk_path(disk_path)
            if target:
                (target_iqn, target_lun) = target
                dev_num = remote_iscsi_initiator.get_device_number_for_target(
                    target_iqn, target_lun)
                disk_path_remote = (
                    vmutils_remote.get_mounted_disk_by_drive_number(dev_num))
                disk_paths_remote[rasd_rel_path] = disk_path_remote
            else:
                LOG.debug("Could not retrieve iSCSI target "
                          "from disk path: %s", disk_path)
        return disk_paths_remote

    def _get_disk_data(self, vm_name, vmutils_remote, disk_path_mapping):
        disk_paths = {}
        phys_disk_resources = vmutils_remote.get_vm_disks(vm_name)[1]

        for disk in phys_disk_resources:
            rasd_rel_path = disk.path().RelPath
            # We set this when volumes are attached.
            serial = disk.ElementName
            disk_paths[rasd_rel_path] = disk_path_mapping[serial]
        return disk_paths

    def _update_planned_vm_disk_resources(self, conn_v2_local,
                                          planned_vm, vm_name,
                                          disk_paths_remote):
        updated_resource_setting_data = []
        sasds = _wqlutils.get_element_associated_class(
            self._compat_conn, self._CIM_RES_ALLOC_SETTING_DATA_CLASS,
            element_uuid=planned_vm.Name)
        for sasd in sasds:
            if (sasd.ResourceType == 17 and sasd.ResourceSubType ==
                    "Microsoft:Hyper-V:Physical Disk Drive" and
                    sasd.HostResource):
                # Replace the local disk target with the correct remote one
                old_disk_path = sasd.HostResource[0]
                new_disk_path = disk_paths_remote.pop(sasd.path().RelPath)

                LOG.debug("Replacing host resource "
                          "%(old_disk_path)s with "
                          "%(new_disk_path)s on planned VM %(vm_name)s",
                          {'old_disk_path': old_disk_path,
                           'new_disk_path': new_disk_path,
                           'vm_name': vm_name})
                sasd.HostResource = [new_disk_path]
                updated_resource_setting_data.append(sasd.GetText_(1))

        LOG.debug("Updating remote planned VM disk paths for VM: %s",
                  vm_name)
        vsmsvc = conn_v2_local.Msvm_VirtualSystemManagementService()[0]
        (res_settings, job_path, ret_val) = vsmsvc.ModifyResourceSettings(
            ResourceSettings=updated_resource_setting_data)
        self._jobutils.check_ret_val(ret_val, job_path)

    def _get_vhd_setting_data(self, vm):
        new_resource_setting_data = []
        sasds = _wqlutils.get_element_associated_class(
            self._compat_conn, self._STORAGE_ALLOC_SETTING_DATA_CLASS,
            element_uuid=vm.Name)
        for sasd in sasds:
            if (sasd.ResourceType == 31 and sasd.ResourceSubType ==
                    "Microsoft:Hyper-V:Virtual Hard Disk"):
                new_resource_setting_data.append(sasd.GetText_(1))
        return new_resource_setting_data

    def _live_migrate_vm(self, conn_v2_local, vm, planned_vm, rmt_ip_addr_list,
                         new_resource_setting_data, dest_host):
        # VirtualSystemAndStorage
        vsmsd = conn_v2_local.query("select * from "
                                    "Msvm_VirtualSystemMigrationSettingData "
                                    "where MigrationType = 32771")[0]
        vsmsd.DestinationIPAddressList = rmt_ip_addr_list
        if planned_vm:
            vsmsd.DestinationPlannedVirtualSystemId = planned_vm.Name
        migration_setting_data = vsmsd.GetText_(1)

        migr_svc = conn_v2_local.Msvm_VirtualSystemMigrationService()[0]

        LOG.debug("Starting live migration for VM: %s", vm.ElementName)
        (job_path, ret_val) = migr_svc.MigrateVirtualSystemToHost(
            ComputerSystem=vm.path_(),
            DestinationHost=dest_host,
            MigrationSettingData=migration_setting_data,
            NewResourceSettingData=new_resource_setting_data)
        self._jobutils.check_ret_val(ret_val, job_path)

    def _get_ip_address_list(self, conn_v2, hostname):
        LOG.debug("Getting live migration networks for host: %s",
                  hostname)
        migr_svc_rmt = conn_v2.Msvm_VirtualSystemMigrationService()[0]
        return migr_svc_rmt.MigrationServiceListenerIPAddressList

    def live_migrate_vm(self, vm_name, dest_host):
        self.check_live_migration_config()

        conn_v2_remote = self._get_conn_v2(dest_host)

        vm = self._get_vm(self._compat_conn, vm_name)

        rmt_ip_addr_list = self._get_ip_address_list(conn_v2_remote,
                                                     dest_host)

        planned_vms = self._get_planned_vms(conn_v2_remote, vm)
        if len(planned_vms) > 1:
            err_msg = _("Multiple planned VMs were found for VM %(vm_name)s "
                        "on host %(dest_host)s")
            raise exceptions.OSWinException(
                err_msg % dict(vm_name=vm_name,
                               dest_host=dest_host))
        elif not planned_vms:
            # TODO(claudiub): Remove this branch after the livemigrationutils
            # usage has been updated to create planned VM on the destination
            # host beforehand.
            planned_vm = None
            disk_paths = self._get_physical_disk_paths(vm_name)
            if disk_paths:
                vmutils_remote = vmutils.VMUtils(dest_host)
                disk_paths_remote = self._get_remote_disk_data(vmutils_remote,
                                                               disk_paths,
                                                               dest_host)
                planned_vm = self._create_planned_vm(conn_v2_remote,
                                                     self._compat_conn,
                                                     vm, rmt_ip_addr_list,
                                                     dest_host)
                self._update_planned_vm_disk_resources(
                    conn_v2_remote, planned_vm, vm_name, disk_paths_remote)
        else:
            planned_vm = planned_vms[0]

        new_resource_setting_data = self._get_vhd_setting_data(vm)
        self._live_migrate_vm(self._compat_conn, vm, planned_vm,
                              rmt_ip_addr_list, new_resource_setting_data,
                              dest_host)

    def create_planned_vm(self, vm_name, src_host, disk_path_mapping):
        # This is run on the destination host.
        dest_host = platform.node()
        vmutils_remote = vmutils.VMUtils(src_host)

        conn_v2_remote = self._get_conn_v2(src_host)
        vm = self._get_vm(conn_v2_remote, vm_name)

        # Make sure there are no planned VMs already.
        self._destroy_existing_planned_vms(self._compat_conn, vm)

        ip_addr_list = self._get_ip_address_list(self._compat_conn,
                                                 dest_host)

        disk_paths = self._get_disk_data(vm_name, vmutils_remote,
                                         disk_path_mapping)

        planned_vm = self._create_planned_vm(self._compat_conn,
                                             conn_v2_remote,
                                             vm, ip_addr_list,
                                             dest_host)
        self._update_planned_vm_disk_resources(self._compat_conn, planned_vm,
                                               vm_name, disk_paths)
