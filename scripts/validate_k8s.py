"""Offline validation of the Kubernetes manifests.

`kubectl --dry-run=client` still contacts a cluster to fetch the OpenAPI
schema, so it cannot run in CI without one. This checks the things that
actually go wrong in practice and needs nothing but a YAML parser:

* every document parses,
* every document has apiVersion/kind/metadata.name,
* every container declares resource requests and limits (a pod without them
  is scheduled best-effort and is evicted first under pressure),
* every long-running workload declares probes,
* no Secret contains a value that looks like a real credential.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

K8S_DIR = Path(__file__).resolve().parents[1] / "k8s"

# Workloads that should always declare probes. Jobs are excluded: they run to
# completion, so a liveness probe would be meaningless.
PROBED_KINDS = {"Deployment", "StatefulSet"}

PLACEHOLDER_MARKERS = ("CHANGE-ME", "changeme", "placeholder")


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    documents = 0

    for path in sorted(K8S_DIR.glob("*.yaml")):
        try:
            docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
        except yaml.YAMLError as exc:
            errors.append(f"{path.name}: does not parse -- {exc}")
            continue

        for doc in docs:
            documents += 1
            name = doc.get("metadata", {}).get("name", "<unnamed>")
            kind = doc.get("kind", "<no kind>")
            label = f"{path.name}:{kind}/{name}"

            for field in ("apiVersion", "kind"):
                if not doc.get(field):
                    errors.append(f"{label}: missing {field}")
            if not doc.get("metadata", {}).get("name"):
                errors.append(f"{label}: missing metadata.name")

            # Namespaced objects should say so explicitly rather than relying
            # on whatever namespace kubectl happens to be pointed at.
            if kind not in ("Namespace",) and not doc.get("metadata", {}).get("namespace"):
                warnings.append(f"{label}: no explicit namespace")

            spec = doc.get("spec", {})
            pod_spec = spec.get("template", {}).get("spec") or (
                spec if kind == "Pod" else None
            )
            if not pod_spec:
                continue

            containers = pod_spec.get("containers", [])
            if not containers:
                errors.append(f"{label}: no containers")

            for container in containers:
                cname = container.get("name", "<unnamed>")
                resources = container.get("resources", {})
                if not resources.get("requests"):
                    errors.append(f"{label}/{cname}: no resource requests")
                if not resources.get("limits"):
                    errors.append(f"{label}/{cname}: no resource limits")

                if kind in PROBED_KINDS:
                    has_probe = any(
                        container.get(p)
                        for p in ("livenessProbe", "readinessProbe", "startupProbe")
                    )
                    if not has_probe:
                        warnings.append(f"{label}/{cname}: no probes declared")

        # Secrets must be obvious placeholders, never real values.
        for doc in docs:
            if doc.get("kind") != "Secret":
                continue
            for key, value in (doc.get("stringData") or {}).items():
                if key in ("POSTGRES_USER",):
                    continue
                if not any(marker in str(value) for marker in PLACEHOLDER_MARKERS):
                    errors.append(
                        f"{path.name}:Secret/{doc['metadata']['name']}: "
                        f"{key} does not look like a placeholder -- never commit a real credential"
                    )

    print(f"Validated {documents} objects across {len(list(K8S_DIR.glob('*.yaml')))} files.")

    for warning in warnings:
        print(f"  WARN  {warning}")
    for error in errors:
        print(f"  ERROR {error}", file=sys.stderr)

    if errors:
        print(f"\n{len(errors)} error(s).", file=sys.stderr)
        return 1

    print(f"\nAll objects valid ({len(warnings)} warning(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
