#!/usr/bin/env python3
"""Inyecta el checksum de los ConfigMaps en el Deployment del router.

Sin esto, un cambio en configmap-router.yaml NO reinicia el pod: el
Deployment pasa a apuntar al ConfigMap nuevo, pero el proceso sigue
sirviendo el codigo viejo que cargo al arrancar. Con la annotation,
Argo CD ve el cambio y hace rollout por si solo.

Uso:
    python3 scripts/gen_checksum.py
"""

import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEPLOYMENT = ROOT / "deployment.yaml"
MANIFESTS = ["configmap-router.yaml", "configmap-sla.yaml"]
ANNOTATION = "backstage.guslopez.dev/config-checksum"


def main() -> None:
    missing = [name for name in MANIFESTS if not (ROOT / name).exists()]
    if missing:
        print("ERROR: faltan %s en %s" % (missing, ROOT), file=sys.stderr)
        sys.exit(1)

    digest = hashlib.sha256()
    for name in MANIFESTS:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update((ROOT / name).read_bytes())
    value = "sha256:" + digest.hexdigest()

    text = DEPLOYMENT.read_text()
    pattern = re.compile(r'(' + re.escape(ANNOTATION) + r':\s*")[^"]*(")')
    if not pattern.search(text):
        print("ERROR: annotation '%s' no encontrada en %s" % (ANNOTATION, DEPLOYMENT),
              file=sys.stderr)
        sys.exit(1)
    DEPLOYMENT.write_text(pattern.sub(r"\g<1>" + value + r"\g<2>", text))
    print("OK  %s  %s" % (DEPLOYMENT, value))


if __name__ == "__main__":
    main()
