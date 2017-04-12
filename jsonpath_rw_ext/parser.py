#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import jsonpath_rw
from jsonpath_rw import lexer
from jsonpath_rw import parser

from jsonpath_rw_ext import _arithmetic
from jsonpath_rw_ext import _filter
from jsonpath_rw_ext import _iterable
from jsonpath_rw_ext import _string

# NOTE(sileht): This block is very important otherwise py3X tests fail no joke
# ply/yacc.py order functions by line, then by module, but in py3 module are
# not sortable, so we add this block to not have methods defined at the same
# line in jsonpath_rw and jsonpath_rw_ext, yes that really sucks ...
# (Need some other lines)
# (Need some other lines)
# (Need some other lines)


class ExtendedJsonPathLexer(lexer.JsonPathLexer):
    """Custom LALR-lexer for JsonPath"""
    literals = lexer.JsonPathLexer.literals + ['?', '@', '+', '*', '/', '-']
    tokens = (parser.JsonPathLexer.tokens +
              ['FILTER_OP', 'SORT_DIRECTION'])

    t_FILTER_OP = r'==?|<=|>=|!=|<|>'

    def t_SORT_DIRECTION(self, t):
        r',?\s*(/|\\)'
        t.value = t.value[-1]
        return t

    def t_ID(self, t):
        r'@?[a-zA-Z_][a-zA-Z0-9_@\-]*'
        # NOTE(sileht): This fixes the ID expression to be
        # able to use @ for `This` like any json query
        t.type = self.reserved_words.get(t.value, 'ID')
        return t


class ExtentedJsonPathParser(parser.JsonPathParser):
    """Custom LALR-parser for JsonPath"""

    tokens = ExtendedJsonPathLexer.tokens

    def __init__(self, debug=False, lexer_class=None):
        lexer_class = lexer_class or ExtendedJsonPathLexer
        super(ExtentedJsonPathParser, self).__init__(debug, lexer_class)

    def p_jsonpath_operator_jsonpath(self, p):
        """jsonpath : NUMBER operator NUMBER
                    | ID operator ID
                    | NUMBER operator jsonpath
                    | jsonpath operator NUMBER
                    | jsonpath operator jsonpath
        """

        # NOTE(sileht): If we have choice between a field or a string we
        # always choice string, because field can be full qualified
        # like $.foo == foo and where string can't.
        for i in [1, 3]:
            if (isinstance(p[i], jsonpath_rw.Fields)
                    and len(p[i].fields) == 1):
                p[i] = p[i].fields[0]

        p[0] = _arithmetic.Operation(p[1], p[2], p[3])

    def p_operator(self, p):
        """operator : '+'
                    | '-'
                    | '*'
                    | '/'
        """
        p[0] = p[1]

    def p_jsonpath_named_operator(self, p):
        "jsonpath : NAMED_OPERATOR"
        if p[1] == 'len':
            p[0] = _iterable.Len()
        elif p[1] == 'sorted':
            p[0] = _iterable.SortedThis()
        elif p[1].startswith("split("):
            p[0] = _string.Split(p[1])
        elif p[1].startswith("sub("):
            p[0] = _string.Sub(p[1])
        else:
            super(ExtentedJsonPathParser, self).p_jsonpath_named_operator(p)

    def p_expression(self, p):
        """expression : jsonpath
                      | jsonpath FILTER_OP ID
                      | jsonpath FILTER_OP NUMBER
        """
        if len(p) == 2:
            left, op, right = p[1], None, None
        else:
            __, left, op, right = p
        p[0] = _filter.Expression(left, op, right)

    def p_expressions_expression(self, p):
        "expressions : expression"
        p[0] = [p[1]]

    def p_expressions_and(self, p):
        "expressions : expressions '&' expressions"
        # TODO(sileht): implements '|'
        p[0] = p[1] + p[3]

    def p_expressions_parens(self, p):
        "expressions : '(' expressions ')'"
        p[0] = p[2]

    def p_filter(self, p):
        "filter : '?' expressions "
        p[0] = _filter.Filter(p[2])

    def p_jsonpath_filter(self, p):
        "jsonpath : jsonpath '[' filter ']'"
        p[0] = jsonpath_rw.Child(p[1], p[3])

    def p_sort(self, p):
        "sort : SORT_DIRECTION jsonpath"
        p[0] = (p[2], p[1] != "/")

    def p_sorts_sort(self, p):
        "sorts : sort"
        p[0] = [p[1]]

    def p_sorts_comma(self, p):
        "sorts : sorts sorts"
        p[0] = p[1] + p[2]

    def p_jsonpath_sort(self, p):
        "jsonpath : jsonpath '[' sorts ']'"
        sort = _iterable.SortedThis(p[3])
        p[0] = jsonpath_rw.Child(p[1], sort)

    def p_jsonpath_this(self, p):
        "jsonpath : '@'"
        p[0] = jsonpath_rw.This()

    precedence = [
        ('left', '+', '-'),
        ('left', '*', '/'),
    ] + jsonpath_rw.parser.JsonPathParser.precedence + [
        ('nonassoc', 'ID'),
    ]


def parse(path, debug=False):
    return ExtentedJsonPathParser(debug=debug).parse(path)