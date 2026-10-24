"""Embed verified repository-owned manifest/bindings for the passive observer."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PINS = {
    'input-actions.json': '4ea6b45b6da23594f806142b017bc6015612753e6c9348d2809eae59d21f1749',
    'frame-controller-bindings.json': '1ba6c9a9596d2cd7fc3459fe1be26762f5ca488128d5346bd934a949acd10fd3',
}
values = []
for name, digest in PINS.items():
    content = (ROOT / 'resources' / name).read_bytes().replace(b'\r\n', b'\n')
    if hashlib.sha256(content).hexdigest() != digest:
        raise SystemExit('repository action resource does not match its reviewed pin: ' + name)
    values.append(content.decode('utf-8'))
output = Path(sys.argv[1])
output.write_text('#pragma once\n#include <string_view>\n'
                  + 'inline constexpr std::string_view pinned_manifest = ' + json.dumps(values[0]) + ';\n'
                  + 'inline constexpr std::string_view pinned_bindings = ' + json.dumps(values[1]) + ';\n', encoding='utf-8')
