#!/usr/bin/env python3
"""Import the pinned local Claude UI capture for `npm run build` in viewer.

Usage: python3 scripts/import_claude_ui.py /path/to/refreshes/2026-09-17
Consumer: viewer's React host. Replaces hand-drawn fonts/theme/primitives.
Copies only verified public client artifacts; does not inspect app/user data.
"""
import hashlib
import json
import pathlib
import re
import sys


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    source = pathlib.Path(sys.argv[1]).resolve()
    viewer = pathlib.Path(__file__).resolve().parents[1] / 'viewer'
    assets = viewer / 'vendor/assets/v1'
    modules = viewer / 'claude'
    assets.mkdir(parents=True, exist_ok=True)
    modules.mkdir(exist_ok=True)
    catalog = {}
    for name in ('claude-cached-web', 'claude-cached-css', 'claude-cached-shared-css'):
        capture = source / name
        manifest = json.loads((capture / 'manifest.json').read_text())
        for row in manifest['assets']:
            if row['ok']:
                catalog[pathlib.Path(row['relativePath']).name] = (capture / row['relativePath'], row)
    names = ['c6a992d55-CGPXYqXR.css', 'shared-styles-BNvA7Nd3.css',
             'shared-frame--1WLhtwS.css', 'c19e90103-B6esh4pG.css']
    pending, seen, receipt = list(names), set(), []
    prefix = 'https://assets-proxy.anthropic.com/claude-ai/v2/assets/v1/'
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        path, row = catalog[name]
        raw = path.read_bytes()
        assert sha(raw) == row['sha256'], path
        output = raw
        if name.endswith('.css'):
            text = raw.decode()
            pending.extend(re.findall(re.escape(prefix) + r'([A-Za-z0-9_.-]+)', text))
            output = text.replace(prefix, '/assets/v1/').encode()
        (assets / name).write_bytes(output)
        receipt.append({'name': name, 'url': row['url'], 'sourceSha256': sha(raw),
                        'localSha256': sha(output), 'bytes': len(output),
                        'change': 'local asset URLs only' if output != raw else 'none'})
    readable = source / 'claude-cached-web/readable/assets-proxy.anthropic.com/claude-ai/v2/assets/v1'
    functions = []

    def extract(name, symbol):
        path = readable / name
        text = path.read_text()
        start = text.index('function ' + symbol + '(')
        end = text.index('\n}', start) + 2
        code = text[start:end]
        functions.append({'file': name, 'symbol': symbol, 'line': text[:start].count('\n') + 1,
                          'sourceSha256': sha(path.read_bytes()), 'functionSha256': sha(code.encode())})
        return code

    frame = (readable / 'shared-frame-CtjUuup5.js').read_text()
    icon_data = frame[frame.index('var Fi = {'):frame.index('\n  Ii =')].rstrip(',') + ';'
    icon_support = frame[frame.index('var ea ='):frame.index('function xa(')]
    header = '''// Generated from the pinned Claude capture. See provenance.json.
import {jsx as q, jsx as M, jsxs as E, jsx as S, jsxs as g} from 'react/jsx-runtime';
const T = (...parts) => parts.filter(Boolean).join(' ');
const Vi = px => `calc(${px / 16}rem * var(--cds-rem-scale, 1))`;
const mi = () => document.documentElement.dataset.density || 'comfortable';
const Il = value => String(value ?? '');
'''
    code = header + icon_data + '\n' + icon_support + extract('shared-frame-CtjUuup5.js', 'xa')
    code += '\nconst Eu = xa;\n' + extract('shared-12-qQd0q2cJ.js', 'MP')
    code += '\n' + extract('cd5a31703-CcopTly8.js', 'Sse')
    code += '''
export {xa as Icon, Sse as FieldList};
export const GroupLabel = props => MP(props, {label: props.children});
'''
    (modules / 'primitives.js').write_text(code)
    # Shiki-compatible Claude themes: preserve the original palette and rules.
    theme_file = readable / 'c5d464fa3-Dgh6wNZX.js'
    theme_source = theme_file.read_text()
    shared = (readable / 'shared-8-BcDmHlOz.js').read_text()
    palette = shared[shared.index('var sP = {'):shared.index('function fP(')]
    tokens = (readable / 'shared-1-Ds2inXNU.js').read_text()
    literals = '\n'.join('const ' + local + ' = ' + re.search(r'\b' + symbol + r' = (\{[^\n]+\})', tokens)[1] + ';'
                         for local, symbol in [('n', 'Qy'), ('r', 'eb')])
    themes = literals + '\n' + palette + '\nconst c = uP;\n'
    themes += theme_source[theme_source.index('var m = "claude-light"'):theme_source.index('function C(')]
    themes += '\nexport {x as lightTheme, S as darkTheme};\n'
    (modules / 'themes.js').write_text(themes)
    # Class recipes are the original TurnStatusStep presentation strings.
    step_file = readable / 'shared-7-XzjbXcab.js'
    steps = step_file.read_text()
    recipes = {name: re.search(r'\b' + symbol + r' =\s*"([^"]+)"', steps)[1]
               for name, symbol in [('outset', 'cA'), ('control', 'lA'), ('row', 'uA'), ('label', 'dA'), ('spacing', 'fA')]}
    (modules / 'recipes.js').write_text('export const step = ' + json.dumps(recipes, indent=2) + ';\n')
    provenance = {'build': 'index-CWWbECaL', 'capture': str(source), 'assets': receipt,
                  'functions': functions, 'adaptations': [
                      'JSX runtime imports replaced with matching React JSX primitives',
                      'class join and text normalization supplied by the Archive host',
                      'density read from the same root data-density attribute',
                      'GroupLabel wrapper supplies the original second argument; body unchanged'],
                  'generatedSha256': sha(code.encode()),
                  'themes': {'file': theme_file.name, 'sourceSha256': sha(theme_file.read_bytes()),
                             'generatedSha256': sha(themes.encode()),
                             'dependencies': ['shared-8-BcDmHlOz.js:sP,lP,uP', 'shared-1-Ds2inXNU.js:Qy,eb']},
                  'stepRecipes': {'file': step_file.name, 'sourceSha256': sha(step_file.read_bytes()),
                                  'symbols': ['cA', 'lA', 'uA', 'dA', 'fA']}}
    (modules / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    print(json.dumps({'assets': len(receipt), 'bytes': sum(x['bytes'] for x in receipt),
                      'functions': [x['symbol'] for x in functions]}))


if __name__ == '__main__':
    main()
