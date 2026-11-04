"""Lexical Java method boundary scan; strings/comments do not affect brace counts."""
import re
TOKEN = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*[\s\S]*?\*/|[{}();]')

def method_window(text, start_line):
    lines = text.splitlines(keepends=True)
    start = sum(map(len, lines[:start_line-1]))
    depth = parens = 0
    opened = False
    end = None
    for token in TOKEN.finditer(text, start):
        value = token.group()
        if len(value) != 1:
            continue
        if not opened:
            if value == '(': parens += 1
            elif value == ')': parens = max(0, parens-1)
            elif value == '{' and parens == 0:
                opened = True
                depth = 1
            elif value == ';' and parens == 0:
                end = token.end()
                break
        else:
            if value == '{': depth += 1
            elif value == '}':
                depth -= 1
                if depth == 0:
                    end = token.end()
                    break
    complete = end is not None
    end = end if complete else min(len(text), start + 12000)
    return dict(text=text[start:end], boundary_found=complete,
                note='Lexical boundary scan from fact location; not a Java AST. Strings and comments preserved. No boundary found uses a bounded fallback.' if not complete else 'Lexical method/declaration boundary from fact location; strings and comments preserved; not a Java AST.')
