"""Deterministic, source-backed component descriptions; no report or GT inputs."""
from collections import Counter, defaultdict
import re
import xml.etree.ElementTree as ET


def compact(text, limit):
    return re.sub(r'\s+', ' ', text).strip()[:limit]


def build_component_record(kb, record):
    component = record['name']
    methods = {m for m, meta in kb.meta.items() if meta['component'] == component}
    package_classes = defaultdict(set)
    for method in methods:
        if method in kb.owners:
            package_classes[kb.meta[method]['package']].add(kb.owners[method])
    package_counts = sorted(package_classes, key=lambda p: (-len(package_classes[p]), p))
    classes = sorted({c for values in package_classes.values() for c in values})
    entry_methods = sorted(methods & kb.entries)
    entry_classes = sorted({kb.owners[m] for m in entry_methods if m in kb.owners})
    # Show representative entry classes first, then breadth across the largest packages.
    examples = []
    for owner in entry_classes:
        if '$anonymous@' not in owner and owner not in examples:
            examples.append(owner)
        if len(examples) == 4:
            break
    for package in package_counts:
        for owner in sorted(package_classes[package]):
            if '$anonymous@' not in owner and owner not in examples:
                examples.append(owner)
                break
        if len(examples) >= 8:
            break
    kinds = Counter(kind for m, kind in kb.facts['EntryKind'] if m in methods)
    sync = Counter(action for m, resource, action, site in kb.facts['Sync'] if m in methods)
    resources = {resource for m, resource, action, site in kb.facts['Access'] if m in methods}
    writes = {resource for m, resource, action, site in kb.facts['Access'] if m in methods and action in ('W','RW')}
    sources = []
    folder = kb.source/component
    purpose = ''
    dependencies = []
    pom = folder/'pom.xml'
    if pom.is_file():
        try:
            doc = ET.parse(pom).getroot()
            name = compact(doc.findtext('{*}name') or doc.findtext('{*}artifactId') or '', 180)
            description = compact(doc.findtext('{*}description') or '', 400)
            purpose = name + (': '+description if description and description != name else '')
            dependencies = sorted({x.findtext('{*}artifactId') for x in doc.findall('{*}dependencies/{*}dependency')
                                   if x.findtext('{*}artifactId') and x.findtext('{*}scope') != 'test'})[:10]
            sources.append(dict(path=pom.relative_to(kb.source).as_posix(), kind='pom_name_description_dependencies'))
        except (ET.ParseError, OSError):
            pass
    excerpt = ''
    for filename in ('README.md','README.rst','README.txt','README'):
        file = folder/filename
        if not file.is_file():
            continue
        with file.open(encoding='utf-8', errors='replace') as stream:
            text = stream.read(12000)
        text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
        text = re.sub(r'```.*?```', '', text, flags=re.S)
        blocks = [compact(block, 500) for block in re.split(r'\n\s*\n', text)
                  if len(compact(block, 500)) > 40 and not block.lstrip().startswith(('#','[!','![','<'))]
        if blocks:
            excerpt = blocks[0]
            sources.append(dict(path=file.relative_to(kb.source).as_posix(), kind='readme_excerpt'))
            break
    knowledge = dict(summary_version=1, purpose_from_build=purpose or None,
                     documentation_excerpt=excerpt or None,
                     package_count=len(package_classes), class_count=len(classes), method_count=len(methods),
                     representative_packages=package_counts[:6], representative_classes=examples[:8],
                     entry_kinds=dict(sorted(kinds.items())), synchronization_actions=dict(sorted(sync.items())),
                     accessed_resource_count=len(resources), written_resource_count=len(writes),
                     dependency_artifacts=dependencies, sources=sources,
                     fact_relations=['InClass', 'Location', 'ThreadEntry', 'EntryKind', 'Access', 'Sync'],
                     limitations='Counts describe extracted MAY facts. Package/class lists are bounded samples, not inferred functional guarantees.')
    sentences = [purpose or component]
    if excerpt:
        sentences.append('Documentation: '+excerpt)
    sentences.append(f'Extracted structure: {len(package_classes)} packages, {len(classes)} classes, {len(methods)} methods.')
    if package_counts:
        sentences.append('Main packages by class count: '+', '.join(package_counts[:6])+'.')
    if examples:
        sentences.append('Representative classes: '+', '.join(examples[:8])+'.')
    sentences.append('Concurrency: '+str(record['evidence']['thread_entries'])+' candidate thread entries; kinds '+str(dict(sorted(kinds.items())))+
                     f'; {len(resources)} accessed resource declarations, {len(writes)} with writes.')
    if sync:
        sentences.append('Synchronization actions: '+str(dict(sorted(sync.items())))+'.')
    if dependencies:
        sentences.append('Declared dependencies: '+', '.join(dependencies)+'.')
    brief = purpose or component
    if excerpt:
        brief += '. ' + excerpt
    elif package_counts:
        brief += '. Main packages: ' + ', '.join(package_counts[:2]) + '.'
    return dict(name=component, description=compact(brief, 280), detailed_description=' '.join(sentences),
                description_source='source documentation + POM + extracted structural/concurrency facts',
                concurrency_evidence=record['evidence'], knowledge=knowledge)
