"""Report anchors: project methods the bug report points at directly, the way a developer starts reading.

Generic, label-free evidence only:
  stack_frame  - `at pkg.Class.method(File.java:N)` mapped to the enclosing project method by file and line
  message      - a project string literal (>= 3 words) that appears in the report, mapped to its enclosing method
  report_call  - `.name(` calls in the report, on classes the report points at (CKB.report_types)
Anchors are expanded along the call graph (callees, overrides that run for report-related subclasses).
"""
import re
from bisect import bisect_right
from collections import defaultdict

FRAME = re.compile(r'\bat\s+([\w$.]+)\.([\w$<>]+)\(([\w$]+\.java)(?::(\d+))?\)')
CALL = re.compile(r'\.\s*([a-zA-Z_$][\w$]*)\s*\(')
LITERAL = re.compile(r'"((?:\\.|[^"\\\n])*)"')


def normalize(text):
    return re.sub(r'\s+', ' ', text).strip().lower()


class AnchorIndex:
    """Per-knowledge-base lookups: methods by file/line and project string literals."""

    def __init__(self, kb):
        self.kb = kb
        self.by_file = defaultdict(list)
        for method, location in kb.locations.items():
            if method in kb.methods:
                file, line, _ = location.rsplit(':', 2)
                self.by_file[file].append((int(line), method))
        for rows in self.by_file.values():
            rows.sort()
        self._literals = None

    def enclosing(self, file, line):
        """Innermost method whose declaration starts at or before line (nested/anonymous starts later)."""
        rows = self.by_file.get(file, [])
        i = bisect_right(rows, (line, '￿'))
        return rows[i-1][1] if i else None

    def literals(self):
        if self._literals is None:
            self._literals = []
            for file in self.by_file:
                path = (self.kb.source / file).resolve()
                if not (path.is_relative_to(self.kb.source) and path.is_file()):
                    continue
                for number, line in enumerate(path.read_text(encoding='utf-8', errors='ignore').splitlines(), 1):
                    for literal in LITERAL.findall(line):
                        # Static text around format placeholders; keep phrases a report could quote.
                        for piece in re.split(r'%[sdf]|\{\}|\\[nt]', literal):
                            piece = normalize(piece.strip(' :,.-=[]()'))
                            if len(piece) >= 15 and len(piece.split()) >= 3:
                                self._literals.append((piece, file, number))
        return self._literals


def frame_anchors(index, report):
    out = {}
    for cls, name, filename, line in FRAME.findall(report):
        # The class's package directory plus the frame's file name (handles inner, anonymous and lambda frames).
        package = cls.split('$')[0].rsplit('.', 1)[0] if '.' in cls else ''
        suffix = '/'.join(package.split('.') + [filename]) if package else filename
        files = [f for f in index.by_file if f.endswith('/' + suffix) or f == suffix]
        for file in files:
            hit = index.enclosing(file, int(line)) if line else None
            simple = name.split('$')[0]
            if hit and (hit.rsplit('#', 1)[1].split('(')[0] == simple or simple.startswith('lambda')):
                out.setdefault(hit, f'stack frame {cls}.{name}:{line}')
                continue
            # Line numbers drift between versions: fall back to same-name methods in that file.
            for _, method in index.by_file[file]:
                if method.rsplit('#', 1)[1].split('(')[0] == simple:
                    out.setdefault(method, f'stack frame {cls}.{name} (by name)')
    return out


def message_anchors(index, report):
    text = normalize(report)
    out = {}
    for piece, file, line in index.literals():
        if piece in text:
            method = index.enclosing(file, line)
            if method:
                out.setdefault(method, f'report quotes literal "{piece[:60]}" at {file}:{line}')
    return out


def call_anchors(kb, report, related=None):
    related = kb.report_types(report) if related is None else related
    names = {n for n in CALL.findall(report) if len(n) > 2}
    out = {}
    for cls, reason in related.items():
        for method in kb.methods_of(cls):
            name = method.rsplit('#', 1)[1].split('(')[0]
            if name in names and not kb.is_declaration(method):
                out.setdefault(method, f'report calls .{name}() on {cls.rsplit(".", 1)[-1]} ({reason})')
    return out


def report_anchors(kb, report, index=None, kinds=('stack_frame', 'message', 'report_call')):
    index = index or AnchorIndex(kb)
    found = {}
    for kind, extract in (('stack_frame', lambda: frame_anchors(index, report)),
                          ('message', lambda: message_anchors(index, report)),
                          ('report_call', lambda: call_anchors(kb, report))):
        if kind in kinds:
            for method, why in extract().items():
                found.setdefault(method, dict(kind=kind, evidence=why))
    return found


def expand(kb, anchors, report, hops=2, callers=False, related=None):
    """Hop distance from the anchors: callees (and overrides that run for report-related
    subclasses), optionally callers. Returns {method: hops}."""
    related = kb.report_types(report) if related is None else related
    reverse = None
    if callers:
        reverse = defaultdict(set)
        for caller in kb.methods:
            for callee in kb.callees(caller):
                reverse[callee].add(caller)
    distance = {m: 0 for m in anchors}
    frontier = list(anchors)
    for hop in range(1, hops + 1):
        following = []
        for method in frontier:
            nexts = set(kb.callees(method))
            nexts |= {o for c in list(nexts) + [method] for o in kb.overrides(c) if o.rsplit('#', 1)[0] in related}
            if reverse is not None:
                nexts |= reverse.get(method, set())
            for other in nexts:
                if other not in distance:
                    distance[other] = hop
                    following.append(other)
        frontier = following
    return distance
