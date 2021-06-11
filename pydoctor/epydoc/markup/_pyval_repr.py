# epydoc -- Marked-up Representations for Python Values
#
# Copyright (C) 2005 Edward Loper
# Author: Edward Loper <edloper@loper.org>
# URL: <http://epydoc.sf.net>
#

"""
Syntax highlighter for Python values.  Currently provides special
colorization support for:

  - lists, tuples, sets, frozensets, dicts
  - numbers
  - strings
  - compiled regexps
  - a variety of AST expressions

The highlighter also takes care of line-wrapping, and automatically
stops generating repr output as soon as it has exceeded the specified
number of lines (which should make it faster than pprint for large
values).  It does I{not} bother to do automatic cycle detection,
because maxlines is typically around 5, so it's really not worth it.

The syntax-highlighted output is encoded using a
L{ParsedDocstring}, which can then be used to generate output in
a variety of formats.
"""

__docformat__ = 'epytext en'

# Implementation note: we use exact tests for classes (list, etc)
# rather than using isinstance, because subclasses might override
# __repr__.

import re
import ast
import functools
import sre_parse, sre_constants
from inspect import BoundArguments, signature
from typing import Any, Callable, Dict, Iterable, Sequence, Union, Optional, List, Tuple, cast, overload

import attr
import astor
from docutils import nodes, utils
from twisted.web.template import Tag

from pydoctor.epydoc.markup import DocstringLinker
from pydoctor.epydoc.markup.restructuredtext import ParsedRstDocstring
from pydoctor.epydoc.docutils import set_node_attributes, wbr, newline, obj_reference
from pydoctor.astutils import node2dottedname, bind_args

def decode_with_backslashreplace(s: bytes) -> str:
    r"""
    Convert the given 8-bit string into unicode, treating any
    character c such that ord(c)<128 as an ascii character, and
    converting any c such that ord(c)>128 into a backslashed escape
    sequence.
        >>> decode_with_backslashreplace('abc\xff\xe8')
        u'abc\\xff\\xe8'
    """
    # s.encode('string-escape') is not appropriate here, since it
    # also adds backslashes to some ascii chars (eg \ and ').

    return (s
            .decode('latin1')
            .encode('ascii', 'backslashreplace')
            .decode('ascii'))

@attr.s(auto_attribs=True)
class _MarkedColorizerState:
    length: int
    charpos: int
    lineno: int
    linebreakok: bool
    score: int

class _ColorizerState:
    """
    An object uesd to keep track of the current state of the pyval
    colorizer.  The L{mark()}/L{restore()} methods can be used to set
    a backup point, and restore back to that backup point.  This is
    used by several colorization methods that first try colorizing
    their object on a single line (setting linebreakok=False); and
    then fall back on a multi-line output if that fails.  The L{score}
    variable is used to keep track of a 'score', reflecting how good
    we think this repr is.  E.g., unhelpful values like '<Foo instance
    at 0x12345>' get low scores.  If the score is too low, we'll use
    the parse-derived repr instead.
    """
    def __init__(self) -> None:
        self.result: List[nodes.Node] = []
        self.charpos = 0
        self.lineno = 1
        self.linebreakok = True

        #: How good this represention is?
        self.score = 0

    def mark(self) -> _MarkedColorizerState:
        return _MarkedColorizerState(
                    length=len(self.result), 
                    charpos=self.charpos,
                    lineno=self.lineno, 
                    linebreakok=self.linebreakok, 
                    score=self.score)

    def restore(self, mark: _MarkedColorizerState) -> None:
        (self.charpos, self.lineno, 
        self.linebreakok, self.score) = (mark.charpos, mark.lineno, 
                                        mark.linebreakok, mark.score)
        del self.result[mark.length:]

class _Maxlines(Exception):
    """A control-flow exception that is raised when PyvalColorizer
    exeeds the maximum number of allowed lines."""

class _Linebreak(Exception):
    """A control-flow exception that is raised when PyvalColorizer
    generates a string containing a newline, but the state object's
    linebreakok variable is False."""

class ColorizedPyvalRepr(ParsedRstDocstring):
    """
    @ivar score: A score, evaluating how good this repr is.
    @ivar is_complete: True if this colorized repr completely describes
       the object.
    """
    def __init__(self, document: nodes.document, score: int, is_complete: bool) -> None:
        super().__init__(document, ())
        self.score = score
        self.is_complete = is_complete
    
    def to_stan(self, docstring_linker: DocstringLinker) -> Tag:
        return Tag('code', children=[super().to_stan(docstring_linker)])

def colorize_pyval(pyval: Any, min_score:Optional[int]=None,
                   linelen:Optional[int]=80, maxlines:int=7, linebreakok:bool=True) -> ColorizedPyvalRepr:
    
    return PyvalColorizer(linelen, maxlines, linebreakok).colorize(
        pyval, min_score)

def colorize_inline_pyval(pyval: Any) -> ColorizedPyvalRepr:
    return colorize_pyval(pyval, linelen=None, linebreakok=False)

class PyvalColorizer:
    """
    Syntax highlighter for Python values.
    """

    def __init__(self, linelen:Optional[int]=75, maxlines:int=5, linebreakok:bool=True):
        self.linelen = linelen
        self.maxlines = maxlines
        self.linebreakok = linebreakok

    #////////////////////////////////////////////////////////////
    # Colorization Tags & other constants
    #////////////////////////////////////////////////////////////

    GROUP_TAG = None # was 'variable-group'     # e.g., "[" and "]"
    COMMA_TAG = None # was 'variable-op'        # The "," that separates elements
    COLON_TAG = None # was 'variable-op'        # The ":" in dictionaries
    CONST_TAG = None                 # None, True, False
    NUMBER_TAG = None                # ints, floats, etc
    QUOTE_TAG = 'variable-quote'     # Quotes around strings.
    STRING_TAG = 'variable-string'   # Body of string literals
    LINK_TAG = 'variable-link'       # Links to other documentables, extracted from AST names and attributes.
    ELLIPSIS_TAG = 'variable-ellipsis'
    LINEWRAP_TAG = 'variable-linewrap'
    UNKNOWN_TAG = 'variable-unknown'

    RE_CHAR_TAG = None
    RE_GROUP_TAG = 're-group'
    RE_REF_TAG = 're-ref'
    RE_OP_TAG = 're-op'
    RE_FLAGS_TAG = 're-flags'

    ELLIPSIS = nodes.inline('...', '...', classes=[ELLIPSIS_TAG])
    LINEWRAP = nodes.inline('', chr(8629), classes=[LINEWRAP_TAG])
    UNKNOWN_REPR = nodes.inline('??', '??', classes=[UNKNOWN_TAG])
    WORD_BREAK_OPPORTUNITY = wbr()
    NEWLINE = newline()

    GENERIC_OBJECT_RE = re.compile(r'^<(?P<descr>.*) at (?P<addr>0x[0-9a-f]+)>$', re.IGNORECASE)

    RE_COMPILE_SIGNATURE = signature(re.compile)

    @staticmethod
    def _str_escape(s: str) -> str:
        def enc(c: str) -> str:
            if c == "'":
                return r"\'"
            elif ord(c) <= 0xff:
                return c.encode('unicode-escape').decode('utf-8')
            else:
                return c
        return ''.join(map(enc, s))

    @staticmethod
    def _bytes_escape(b: bytes) -> str:
        return repr(b)[2:-1]

    #////////////////////////////////////////////////////////////
    # Entry Point
    #////////////////////////////////////////////////////////////

    def colorize(self, pyval: Any, min_score: Optional[int] = None) -> ColorizedPyvalRepr:
        """
        @return: A L{ColorizedPyvalRepr} describing the given pyval.
        """

        # Create an object to keep track of the colorization.
        state = _ColorizerState()
        state.linebreakok = self.linebreakok
        # Colorize the value.  If we reach maxlines, then add on an
        # ellipsis marker and call it a day.
        try:
            self._colorize(pyval, state)
        except (_Maxlines, _Linebreak):
            if self.linebreakok:
                state.result.append(self.NEWLINE)
                state.result.append(self.ELLIPSIS)
            else:
                if state.result[-1] is self.LINEWRAP:
                    state.result.pop()
                self._trim_result(state.result, 3)
                state.result.append(self.ELLIPSIS)
            is_complete = False
        else:
            is_complete = True
        
        # If we didn't score high enough, then use UNKNOWN_REPR
        if (min_score is not None and state.score < min_score):
            state.result = [PyvalColorizer.UNKNOWN_REPR]
        
        # Put it all together.
        document = utils.new_document('epytext')
        set_node_attributes(document, document=document, children=state.result)
        return ColorizedPyvalRepr(document, state.score, is_complete)
    
    def _colorize(self, pyval: Any, state: _ColorizerState) -> None:
        pyval_type = type(pyval)
        state.score += 1
        
        if pyval in (False, True, None, NotImplemented):
            # Link built-in constants to the standard library.
            # Ellipsis is not included here, both because its code syntax is
            # different from its constant's name and because its documentation
            # is not relevant to annotations.
            self._output(str(pyval), self.CONST_TAG, state, link=True)
        elif issubclass(pyval_type, (int, float, complex)):
            self._output(str(pyval), self.NUMBER_TAG, state)
        elif issubclass(pyval_type, str):
            self._colorize_str(pyval, state, '', escape_fcn=self._str_escape, str_func=str)
        elif issubclass(pyval_type, bytes):
            self._colorize_str(pyval, state, b'b', escape_fcn=self._bytes_escape, 
                               str_func=functools.partial(bytes, encoding='utf-8', errors='replace'))
        elif issubclass(pyval_type, tuple):
            self._multiline(self._colorize_iter, pyval, state, prefix='(', suffix=')')
        elif issubclass(pyval_type, set):
            self._multiline(self._colorize_iter, pyval,
                            state, prefix='set([', suffix='])')
        elif issubclass(pyval_type, frozenset):
            self._multiline(self._colorize_iter, pyval,
                            state, prefix='frozenset([', suffix='])')
        elif issubclass(pyval_type, dict):
            self._multiline(self._colorize_dict,
                            list(pyval.items()),
                            state, prefix='{', suffix='}')
        elif issubclass(pyval_type, list):
            self._multiline(self._colorize_iter, pyval, state, prefix='[', suffix=']')
        elif issubclass(pyval_type, re.Pattern):
            # Extract the flag & pattern from the regexp.
            self._colorize_re(pyval.pattern, pyval.flags, state)
        elif issubclass(pyval_type, ast.AST):
            self._colorize_ast(pyval, state)
        else:
            try:
                pyval_repr = repr(pyval)
                if not isinstance(pyval_repr, str):
                    pyval_repr = str(pyval_repr) #type: ignore[unreachable]
            except KeyboardInterrupt:
                raise
            except:
                state.score -= 100
                state.result.append(self.UNKNOWN_REPR)
            else:
                match = self.GENERIC_OBJECT_RE.search(pyval_repr)
                if match:
                    state.score -= 5
                    generic_object_match_groups = match.groupdict()
                    if 'descr' in generic_object_match_groups:
                        pyval_repr = f"<{generic_object_match_groups['descr']}>"
                        self._output(pyval_repr, None, state)
                    else:
                        state.result.append(self.UNKNOWN_REPR)
                else:
                    self._output(pyval_repr, None, state)

    def _trim_result(self, result: List[nodes.Node], num_chars: int) -> None:
        while num_chars > 0:
            if not result: 
                return
            if isinstance(result[-1], nodes.Element):
                if len(result[-1].children) >= 1:
                    data = result[-1][-1].astext()
                    trim = min(num_chars, len(data))
                    result[-1][-1] = nodes.Text(data[:-trim])
                    if not result[-1][-1].astext(): 
                        if len(result[-1].children) == 1:
                            result.pop()
                        else:
                            result[-1].pop()
                else:
                    trim = 0
                    result.pop()
                num_chars -= trim
            else:
                # Must be Text if it's not an Element
                trim = min(num_chars, len(result[-1]))
                result[-1] = nodes.Text(result[-1].astext()[:-trim])
                if not result[-1].astext(): 
                    result.pop()
                num_chars -= trim

    #////////////////////////////////////////////////////////////
    # Object Colorization Functions
    #////////////////////////////////////////////////////////////

    def _insert_comma(self, indent: int, state: _ColorizerState) -> None:
        if state.linebreakok:
            self._output(',', self.COMMA_TAG, state)
            self._output('\n'+' '*indent, None, state)
        else:
            self._output(', ', self.COMMA_TAG, state)

    def _multiline(self, func: Callable[..., None], pyval: Iterable[Any], state: _ColorizerState, **kwargs: Any) -> None:
        """
        Helper for container-type colorizers.  First, try calling
        C{func(pyval, state, **kwargs)} with linebreakok set to false;
        and if that fails, then try again with it set to true.
        """
        linebreakok = state.linebreakok
        mark = state.mark()

        try:
            state.linebreakok = False
            func(pyval, state, **kwargs)
            state.linebreakok = linebreakok

        except _Linebreak:
            if not linebreakok:
                raise
            state.restore(mark)
            func(pyval, state, **kwargs)

    def _colorize_iter(self, pyval: Iterable[Any], state: _ColorizerState, 
                       prefix: Optional[Union[str, bytes]] = None, 
                       suffix: Optional[Union[str, bytes]] = None) -> None:
        if prefix is not None:
            self._output(prefix, self.GROUP_TAG, state)
        indent = state.charpos
        for i, elt in enumerate(pyval):
            if i>=1:
                self._insert_comma(indent, state)
            # word break opportunity for inline values
            state.result.append(self.WORD_BREAK_OPPORTUNITY)
            self._colorize(elt, state)
        if suffix is not None:
            self._output(suffix, self.GROUP_TAG, state)

    def _colorize_dict(self, items: Iterable[Tuple[Any, Any]], state: _ColorizerState, prefix: str, suffix: str) -> None:
        self._output(prefix, self.GROUP_TAG, state)
        indent = state.charpos
        for i, (key, val) in enumerate(items):
            if i>=1:
                if state.linebreakok:
                    self._output(',', self.COMMA_TAG, state)
                    self._output('\n'+' '*indent, None, state)
                else:
                    self._output(', ', self.COMMA_TAG, state)
            state.result.append(self.WORD_BREAK_OPPORTUNITY)
            self._colorize(key, state)
            self._output(': ', self.COLON_TAG, state)
            self._colorize(val, state)
        self._output(suffix, self.GROUP_TAG, state)
    
    @overload
    def _colorize_str(self, pyval: str, state: _ColorizerState, prefix: str, 
        escape_fcn: Callable[[str], str], str_func: Callable[[str], str]) -> None: 
        ...
    @overload
    def _colorize_str(self, pyval: bytes, state: _ColorizerState, prefix: bytes, 
        escape_fcn: Callable[[bytes], str], str_func: Callable[[str], bytes]) -> None: 
        ...
    def _colorize_str(self, pyval, state, prefix, escape_fcn, str_func) -> None: #type: ignore[no-untyped-def]
        
        # TODO: Double check implementation bytes/str

        #  Decide which quote to use.
        if str_func('\n') in pyval and state.linebreakok: 
            quote = str_func("'''")
        else: 
            quote = str_func("'")
        # Divide the string into lines.
        if state.linebreakok:
            lines: List[Union[str, bytes]] = pyval.split(str_func('\n'))
        else:
            lines = [pyval]
        # Open quote.
        self._output(prefix+quote, self.QUOTE_TAG, state)
        # Body
        for i, line in enumerate(lines):
            if i>0: 
                self._output(str_func('\n'), None, state)
            if escape_fcn:
                line = escape_fcn(line)
            self._output(line, self.STRING_TAG, state)
        # Close quote.
        self._output(quote, self.QUOTE_TAG, state)

    #////////////////////////////////////////////////////////////
    # Support for AST
    #////////////////////////////////////////////////////////////

    # TODO: Add support for comparators and generator expressions.

    @staticmethod
    def _is_ast_constant(node: ast.AST) -> bool:
        return isinstance(node, (ast.Num, ast.Str, ast.Bytes, 
                                 ast.Constant, ast.NameConstant, ast.Ellipsis))
    @staticmethod
    def _get_ast_constant_val(node: ast.AST) -> Any:
        # Deprecated since version 3.8: Replaced by Constant
        if isinstance(node, ast.Num): 
            return(node.n)
        if isinstance(node, (ast.Str, ast.Bytes)):
           return(node.s)
        if isinstance(node, (ast.Constant, ast.NameConstant)):
            return(node.value)
        if isinstance(node, ast.Ellipsis):
            return(...)
        
    def _colorize_ast_constant(self, pyval: ast.AST, state: _ColorizerState) -> None:
        val = self._get_ast_constant_val(pyval)
        # Handle elipsis
        if val != ...:
            self._colorize(val, state)
        else:
            self._output('...', self.ELLIPSIS_TAG, state)

    def _colorize_ast(self, pyval: ast.AST, state: _ColorizerState) -> None:

        if self._is_ast_constant(pyval): 
            self._colorize_ast_constant(pyval, state)
        elif isinstance(pyval, ast.UnaryOp):
            self._colorize_ast_unary_op(pyval, state)
        elif isinstance(pyval, ast.BinOp):
            self._colorize_ast_binary_op(pyval, state)
        elif isinstance(pyval, ast.BoolOp):
            self._colorize_ast_bool_op(pyval, state)
        elif isinstance(pyval, ast.List):
            self._multiline(self._colorize_iter, pyval.elts, state, prefix='[', suffix=']')
        elif isinstance(pyval, ast.Tuple):
            self._multiline(self._colorize_iter, pyval.elts, state, prefix='(', suffix=')')
        elif isinstance(pyval, ast.Set):
            self._multiline(self._colorize_iter, pyval.elts, state, prefix='set([', suffix='])')
        elif isinstance(pyval, ast.Dict):
            items = list(zip(pyval.keys, pyval.values))
            self._multiline(self._colorize_dict, items, state, prefix='{', suffix='}')
        elif isinstance(pyval, ast.Name):
            self._colorize_ast_name(pyval, state)
        elif isinstance(pyval, ast.Attribute):
            self._colorize_ast_attribute(pyval, state)
        elif isinstance(pyval, ast.Subscript):
            self._colorize_ast_subscript(pyval, state)
        elif isinstance(pyval, ast.Call):
            self._colorize_ast_call(pyval, state)
        elif isinstance(pyval, ast.Starred):
            self._output('*', None, state)
            self._colorize_ast(pyval.value, state)
        elif isinstance(pyval, ast.keyword):
            if pyval.arg is not None:
                self._output(pyval.arg, None, state)
                self._output('=', None, state)
            else:
                self._output('**', None, state)
            self._colorize_ast(pyval.value, state)
        else:
            self._colorize_ast_generic(pyval, state)
    
    def _colorize_ast_unary_op(self, pyval: ast.UnaryOp, state: _ColorizerState) -> None:
        if isinstance(pyval.op, ast.USub):
            self._output('-', None, state)
        elif isinstance(pyval.op, ast.UAdd):
            self._output('+', None, state)
        elif isinstance(pyval.op, ast.Not):
            self._output('not ', None, state)
        elif isinstance(pyval.op, ast.Invert):
            self._output('~', None, state)

        # self._output(astor.to_source(pyval.op), None, state)
        # if isinstance(pyval.op, ast.Not):
        #     self._output(' ', None, state)

        self._colorize(pyval.operand, state)
    
    def _colorize_ast_binary_op(self, pyval: ast.BinOp, state: _ColorizerState) -> None:
        # Colorize first operand
        self._colorize(pyval.left, state)

        # Colorize operator
        if isinstance(pyval.op, ast.Sub):
            self._output('-', None, state)
        elif isinstance(pyval.op, ast.Add):
            self._output('+', None, state)
        elif isinstance(pyval.op, ast.Mult):
            self._output('*', None, state)
        elif isinstance(pyval.op, ast.Div):
            self._output('/', None, state)
        elif isinstance(pyval.op, ast.FloorDiv):
            self._output('//', None, state)
        elif isinstance(pyval.op, ast.Mod):
            self._output('%', None, state)
        elif isinstance(pyval.op, ast.Pow):
            self._output('**', None, state)
        elif isinstance(pyval.op, ast.LShift):
            self._output('<<', None, state)
        elif isinstance(pyval.op, ast.RShift):
            self._output('>>', None, state)
        elif isinstance(pyval.op, ast.BitOr):
            self._output('|', None, state)
        elif isinstance(pyval.op, ast.BitXor):
            self._output('^', None, state)
        elif isinstance(pyval.op, ast.BitAnd):
            self._output('&', None, state)
        elif isinstance(pyval.op, ast.MatMult):
            self._output('@', None, state)
        else:
            self._colorize_ast_generic(pyval, state)

        # Colorize second operand
        self._colorize(pyval.right, state)
    
    def _colorize_ast_bool_op(self, pyval: ast.BoolOp, state: _ColorizerState) -> None:
        _maxindex = len(pyval.values)-1

        for index, value in enumerate(pyval.values):
            self._colorize(value, state)

            if index != _maxindex:
                # self._output(f' {astor.to_source(pyval.op)} ', None, state)
                if isinstance(pyval.op, ast.And):
                    self._output(' and ', None, state)
                elif isinstance(pyval.op, ast.Or):
                    self._output(' or ', None, state)

    def _colorize_ast_name(self, pyval: ast.Name, state: _ColorizerState) -> None:
        self._output(pyval.id, self.LINK_TAG, state, link=True)

    def _colorize_ast_attribute(self, pyval: ast.Attribute, state: _ColorizerState) -> None:
        parts = []
        curr: ast.expr = pyval
        while isinstance(curr, ast.Attribute):
            parts.append(curr.attr)
            curr = curr.value
        if not isinstance(curr, ast.Name):
            self._colorize_ast_generic(pyval, state)
            return
        parts.append(curr.id)
        parts.reverse()
        self._output('.'.join(parts), self.LINK_TAG, state, link=True)

    def _colorize_ast_subscript(self, node: ast.Subscript, state: _ColorizerState) -> None:

        self._colorize(node.value, state)

        sub: ast.AST = node.slice
        if isinstance(sub, ast.Index):
            # In Python < 3.9, non-slices are always wrapped in an Index node.
            sub = sub.value
        self._output('[', self.GROUP_TAG, state)
        if isinstance(sub, ast.Tuple):
            self._multiline(self._colorize_iter, sub.elts, state)
        elif isinstance(sub, (ast.Slice, ast.ExtSlice)):
            state.result.append(self.WORD_BREAK_OPPORTUNITY)
            self._colorize_ast_generic(sub, state)
        else:
            state.result.append(self.WORD_BREAK_OPPORTUNITY)
            self._colorize_ast(sub, state)
       
        self._output(']', self.GROUP_TAG, state)
    
    def _colorize_ast_call(self, node: ast.Call, state: _ColorizerState) -> None:
        
        if node2dottedname(node.func) == ['re', 'compile']:
            # Colorize regexps from re.compile AST arguments.
            try:
                # Can raise TypeError
                args = bind_args(self.RE_COMPILE_SIGNATURE, node)
            except TypeError:
                self._colorize_ast_call_generic(node, state)
            else:
                mark = state.mark()
                try:
                    # Can raise ValueError or re.error
                    self._colorize_ast_re(args, node, state)
                except (ValueError, re.error):
                    state.restore(mark)
                    self._colorize_ast_call_generic(args, node, state)
        else:
            # Colorize other forms of callables.
            self._colorize_ast_call_generic(node, state)

    def _colorize_ast_call_generic(self, node: ast.Call, state: _ColorizerState) -> None:
        self._colorize_ast(node.func, state)
        self._output('(', self.GROUP_TAG, state)
        indent = state.charpos
        self._multiline(self._colorize_iter, node.args, state)
        if node.keywords:
            if node.args:
                self._insert_comma(indent, state)
            self._multiline(self._colorize_iter, node.keywords, state)
        self._output(')', self.GROUP_TAG, state)

    def _colorize_ast_re(self, args: BoundArguments, node:ast.Call, state: _ColorizerState) -> None:
        ast_pattern = args.arguments.get('pattern')
        if ast_pattern is not None:
            if self._is_ast_constant(ast_pattern):
                self._colorize_ast(node.func, state)
                self._output('(', self.GROUP_TAG, state)
                indent = state.charpos
                self._output('r', None, state)
                self._output("'", self.QUOTE_TAG, state)
                pat = self._get_ast_constant_val(ast_pattern)
                self._colorize_re_pattern(pat, 0, state)
                self._output("'", self.QUOTE_TAG, state)
                ast_flags = args.arguments.get('flags')
                if ast_flags is not None:
                    self._insert_comma(indent, state)
                    self._colorize_ast(ast_flags, state)
                self._output(')', self.GROUP_TAG, state)
            else: 
                self._colorize_ast_call_generic(node, state)
        else:
            self._colorize_ast_call_generic(node, state)

    def _colorize_ast_generic(self, pyval: ast.AST, state: _ColorizerState) -> None:
        try:
            source = astor.to_source(pyval)
        except Exception: #  No defined handler for node of type <type>
            state.result.append(self.UNKNOWN_REPR)
        else:
            # TODO: Maybe try to colorize anyway, without links, with epydoc.doctest ?
            self._output(source, None, state)
        
    #////////////////////////////////////////////////////////////
    # Support for Regexes
    #////////////////////////////////////////////////////////////

    def _colorize_re(self, pat: Union[str, bytes], flags: int, state: _ColorizerState) -> None:
        # This method is only used for live re.Pattern objects
        self._output("re.compile(r'", None, state)
        self._colorize_re_flags(flags, state)
        self._colorize_re_pattern(pat, flags, state)
        self._output("')", None, state)
    
    def _colorize_re_pattern(self, pat: Union[str, bytes], flags: int, state: _ColorizerState) -> None:
        # Parse the regexp pattern.
        if isinstance(pat, bytes):
            pat = pat.decode()
        tree: sre_parse.SubPattern = sre_parse.parse(pat, flags)
        groups = dict([(num,name) for (name,num) in
                       tree.state.groupdict.items()])
        # Colorize it!
        self._colorize_re_tree(tree.data, state, True, groups)

    def _colorize_re_flags(self, flags: int, state: _ColorizerState) -> None:
        if flags:
            flags_list = [c for (c,n) in sorted(sre_parse.FLAGS.items())
                     if (n&flags)]
            flags_str = '(?%s)' % ''.join(flags_list)
            self._output(flags_str, self.RE_FLAGS_TAG, state)

    def _colorize_re_tree(self, tree: Sequence[Tuple[sre_constants._NamedIntConstant, Any]],
                          state: _ColorizerState, noparen: bool, groups: Dict[int, str]) -> None:
        
        # TODO: Double check is it necessary ?
        b: Callable[..., bytes] = functools.partial(bytes, encoding='utf-8', errors='replace')

        if len(tree) > 1 and not noparen:
            self._output('(', self.RE_GROUP_TAG, state)

        for elt in tree:
            op = elt[0]
            args = elt[1]

            if op == sre_constants.LITERAL:
                c: Union[str, bytes] = chr(cast(int, args))
                # Add any appropriate escaping.
                if cast(str, c) in '.^$\\*+?{}[]|()\'': 
                    c = b'\\' + b(c)
                elif c == '\t': 
                    c = '\\t'
                elif c == '\r': 
                    c = '\\r'
                elif c == '\n': 
                    c = '\\n'
                elif c == '\f': 
                    c = '\\f'
                elif c == '\v': 
                    c = '\\v'
                elif ord(c) > 0xffff: 
                    c = b(r'\U%08x') % ord(c)
                elif ord(c) > 0xff: 
                    c = b(r'\u%04x') % ord(c)
                elif ord(c)<32 or ord(c)>=127: 
                    c = b(r'\x%02x') % ord(c)
                self._output(c, self.RE_CHAR_TAG, state)

            elif op == sre_constants.ANY:
                self._output('.', self.RE_CHAR_TAG, state)

            elif op == sre_constants.BRANCH:
                if args[0] is not None:
                    raise ValueError('Branch expected None arg but got %s'
                                     % args[0])
                for i, item in enumerate(args[1]):
                    if i > 0:
                        self._output('|', self.RE_OP_TAG, state)
                    self._colorize_re_tree(item, state, True, groups)

            elif op == sre_constants.IN:
                if (len(args) == 1 and args[0][0] == sre_constants.CATEGORY):
                    self._colorize_re_tree(args, state, False, groups)
                else:
                    self._output('[', self.RE_GROUP_TAG, state)
                    self._colorize_re_tree(args, state, True, groups)
                    self._output(']', self.RE_GROUP_TAG, state)

            elif op == sre_constants.CATEGORY:
                if args == sre_constants.CATEGORY_DIGIT: val = b(r'\d')
                elif args == sre_constants.CATEGORY_NOT_DIGIT: val = b(r'\D')
                elif args == sre_constants.CATEGORY_SPACE: val = b(r'\s')
                elif args == sre_constants.CATEGORY_NOT_SPACE: val = b(r'\S')
                elif args == sre_constants.CATEGORY_WORD: val = b(r'\w')
                elif args == sre_constants.CATEGORY_NOT_WORD: val = b(r'\W')
                else: raise ValueError('Unknown category %s' % args)
                self._output(val, self.RE_CHAR_TAG, state)

            elif op == sre_constants.AT:
                if args == sre_constants.AT_BEGINNING_STRING: val = b(r'\A')
                elif args == sre_constants.AT_BEGINNING: val = b(r'^')
                elif args == sre_constants.AT_END: val = b(r'$')
                elif args == sre_constants.AT_BOUNDARY: val = b(r'\b')
                elif args == sre_constants.AT_NON_BOUNDARY: val = b(r'\B')
                elif args == sre_constants.AT_END_STRING: val = b(r'\Z')
                else: raise ValueError('Unknown position %s' % args)
                self._output(val, self.RE_CHAR_TAG, state)

            elif op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT):
                minrpt = args[0]
                maxrpt = args[1]
                if maxrpt == sre_constants.MAXREPEAT:
                    if minrpt == 0:   val = b('*')
                    elif minrpt == 1: val = b('+')
                    else: val = b('{%d,}') % (minrpt)
                elif minrpt == 0:
                    if maxrpt == 1: val = b('?')
                    else: val = b('{,%d}') % (maxrpt)
                elif minrpt == maxrpt:
                    val = b('{%d}') % (maxrpt)
                else:
                    val = b('{%d,%d}') % (minrpt, maxrpt)
                if op == sre_constants.MIN_REPEAT:
                    val += b('?')

                self._colorize_re_tree(args[2], state, False, groups)
                self._output(val, self.RE_OP_TAG, state)

            elif op == sre_constants.SUBPATTERN:
                if args[0] is None:
                    self._output(b('(?:'), self.RE_GROUP_TAG, state)
                elif args[0] in groups:
                    self._output(b('(?P<'), self.RE_GROUP_TAG, state)
                    self._output(groups[args[0]], self.RE_REF_TAG, state)
                    self._output(b('>'), self.RE_GROUP_TAG, state)
                elif isinstance(args[0], int):
                    # This is cheating:
                    self._output(b('('), self.RE_GROUP_TAG, state)
                else:
                    self._output(b('(?P<'), self.RE_GROUP_TAG, state)
                    self._output(args[0], self.RE_REF_TAG, state)
                    self._output(b('>'), self.RE_GROUP_TAG, state)
                self._colorize_re_tree(args[3], state, True, groups)
                self._output(b(')'), self.RE_GROUP_TAG, state)

            elif op == sre_constants.GROUPREF:
                self._output(b('\\%d') % args, self.RE_REF_TAG, state)

            elif op == sre_constants.RANGE:
                self._colorize_re_tree( ((sre_constants.LITERAL, args[0]),),
                                        state, False, groups )
                self._output(b('-'), self.RE_OP_TAG, state)
                self._colorize_re_tree( ((sre_constants.LITERAL, args[1]),),
                                        state, False, groups )

            elif op == sre_constants.NEGATE:
                self._output(b('^'), self.RE_OP_TAG, state)

            elif op == sre_constants.ASSERT:
                if args[0] > 0:
                    self._output(b('(?='), self.RE_GROUP_TAG, state)
                else:
                    self._output(b('(?<='), self.RE_GROUP_TAG, state)
                self._colorize_re_tree(args[1], state, True, groups)
                self._output(b(')'), self.RE_GROUP_TAG, state)

            elif op == sre_constants.ASSERT_NOT:
                if args[0] > 0:
                    self._output(b('(?!'), self.RE_GROUP_TAG, state)
                else:
                    self._output(b('(?<!'), self.RE_GROUP_TAG, state)
                self._colorize_re_tree(args[1], state, True, groups)
                self._output(b(')'), self.RE_GROUP_TAG, state)

            elif op == sre_constants.NOT_LITERAL:
                self._output(b('[^'), self.RE_GROUP_TAG, state)
                self._colorize_re_tree( ((sre_constants.LITERAL, args),),
                                        state, False, groups )
                self._output(b(']'), self.RE_GROUP_TAG, state)
            else:
                raise RuntimeError(f"Error colorizing regexp, unknown element :{elt}")
        if len(tree) > 1 and not noparen:
            self._output(b(')'), self.RE_GROUP_TAG, state)

    #////////////////////////////////////////////////////////////
    # Output function
    #////////////////////////////////////////////////////////////

    def _output(self, s: Union[str, bytes], css_class: Optional[str], 
                state: _ColorizerState, link: bool = False) -> None:
        """
        Add the string `s` to the result list, tagging its contents
        with css class `css_class`.  Any lines that go beyond `self.linelen` will
        be line-wrapped.  If the total number of lines exceeds
        `self.maxlines`, then raise a `_Maxlines` exception.
        """
        # Make sure the string is unicode.
        if isinstance(s, bytes):
            s = decode_with_backslashreplace(s)

        # Split the string into segments.  The first segment is the
        # content to add to the current line, and the remaining
        # segments are new lines.
        segments = s.split('\n')

        for i, segment in enumerate(segments):
            # If this isn't the first segment, then add a newline to
            # split it from the previous segment.
            if i > 0:
                if (state.lineno+1) > self.maxlines:
                    raise _Maxlines()
                if not state.linebreakok:
                    raise _Linebreak()
                state.result.append(self.NEWLINE)
                state.lineno += 1
                state.charpos = 0
            
            segment_len = len(segment) 

            # If the segment fits on the current line, then just call
            # markup to tag it, and store the result.
            # Don't break links into separate segments. 
            if (self.linelen is None or 
                state.charpos + segment_len <= self.linelen or link):
                state.charpos += segment_len
                
                if css_class is not None or link:
                    if link:
                        element = obj_reference('', segment, refuid=segment)
                    else:
                        element = nodes.inline('', segment, classes=[css_class])
                else:
                    element = nodes.Text(segment)
                state.result.append(element)

            # If the segment doesn't fit on the current line, then
            # line-wrap it, and insert the remainder of the line into
            # the segments list that we're iterating over.  (We'll go
            # the the beginning of the next line at the start of the
            # next iteration through the loop.)
            else:
                assert isinstance(self.linelen, int)
                split = self.linelen-state.charpos
                segments.insert(i+1, segment[split:])
                segment = segment[:split]

                if css_class is not None:
                    element = nodes.inline('', segment, classes=[css_class])
                else:
                    element = nodes.Text(segment)
                state.result += [element, self.LINEWRAP]
	