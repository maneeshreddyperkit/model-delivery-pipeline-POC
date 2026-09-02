"""Stage 2 - validate.

Rejects input that would otherwise become a bad delivery. The split
between this stage and the QA stage is deliberate:

  validate - is this file admissible at all? Failures are hard stops,
             because nothing downstream can produce a correct result.
  qa       - the model converted; is the *output* fit to publish?

Findings that are recoverable are recorded as warnings and carried
forward rather than failing the job, so one non-standard tag does not
block a 3,000-component model from reaching its users.
"""

from __future__ import annotations

import re

from ..errors import ValidationFailed
from .base import JobContext, Stage


class ValidateStage(Stage):
    name = "validate"

    def run(self, ctx: JobContext) -> dict:
        self.check_fault_injection(ctx)

        source = ctx.state["source_model"]
        manifest = source.manifest
        rules = ctx.config["validation"]
        warnings: list[dict] = []

        # --- manifest completeness ---------------------------------------
        missing = [f for f in rules["required_manifest_fields"] if not manifest.get(f)]
        if missing:
            raise ValidationFailed(
                f"Manifest is missing required field(s): {', '.join(missing)}. "
                f"Without these the model cannot be routed to a project or "
                f"placed in the correct coordinate space.",
                detail={"missing": missing, "present": sorted(manifest)},
            )

        # --- units --------------------------------------------------------
        units = str(manifest.get("unit_system", "")).lower()
        if units not in rules["allowed_unit_systems"]:
            raise ValidationFailed(
                f"Unit system {manifest.get('unit_system')!r} is not accepted. "
                f"Allowed: {', '.join(rules['allowed_unit_systems'])}.",
                detail={"unit_system": manifest.get("unit_system")},
            )
        if units != "mm":
            # Wrong-but-known units are a conversion problem, not a rejection.
            # Publishing a model at 25.4x the correct scale is far worse than
            # failing, so this is a hard stop until a scaling rule is agreed.
            raise ValidationFailed(
                f"Model is exported in {units!r}. This delivery pipeline "
                f"publishes in millimetres; a unit conversion rule must be "
                f"configured for this project before it can be published, "
                f"otherwise the model lands in the viewer at the wrong scale.",
                detail={"unit_system": units, "expected": "mm"},
            )

        # --- discipline ----------------------------------------------------
        discipline = str(manifest.get("discipline", "")).upper()
        if discipline not in rules["allowed_disciplines"]:
            warnings.append({
                "rule_code": "UNKNOWN_DISCIPLINE",
                "severity": "WARN",
                "message": f"Discipline {discipline!r} is not in the configured list; "
                           f"the model will publish but will not group correctly in "
                           f"discipline filters.",
            })

        # --- volume limits --------------------------------------------------
        if len(source.components) > rules["max_components"]:
            raise ValidationFailed(
                f"Model has {len(source.components):,} components, above the "
                f"configured ceiling of {rules['max_components']:,}.",
                detail={"components": len(source.components)},
            )
        source_mb = ctx.source_path.stat().st_size / (1024 * 1024)
        if source_mb > rules["max_source_mb"]:
            raise ValidationFailed(
                f"Source file is {source_mb:.1f} MB, above the configured "
                f"ceiling of {rules['max_source_mb']} MB.",
                detail={"source_mb": round(source_mb, 1)},
            )
        if not source.components:
            raise ValidationFailed(
                "Model contains no components.", detail={"components": 0})

        # --- tag naming standard ---------------------------------------------
        pattern = re.compile(rules["tag_pattern"])
        bad_tags = [c.tag for c in source.components if not pattern.match(c.tag)]
        if bad_tags:
            warnings.append({
                "rule_code": "TAG_FORMAT",
                "severity": "WARN",
                "message": f"{len(bad_tags)} component tag(s) do not match the project "
                           f"naming standard, e.g. {', '.join(bad_tags[:3])}. These "
                           f"will not resolve against tag-based lookups downstream.",
                "sample": bad_tags[:20],
            })

        # --- duplicate tags -----------------------------------------------------
        seen: dict[str, int] = {}
        for comp in source.components:
            seen[comp.tag] = seen.get(comp.tag, 0) + 1
        duplicates = {t: n for t, n in seen.items() if n > 1}
        if duplicates:
            warnings.append({
                "rule_code": "DUPLICATE_TAG",
                "severity": "ERROR",
                "message": f"{len(duplicates)} tag(s) appear more than once. Tag "
                           f"lookups and attribute joins are ambiguous for these "
                           f"components, e.g. {', '.join(list(duplicates)[:3])}.",
                "sample": [{"tag": t, "count": n} for t, n in list(duplicates.items())[:20]],
            })

        # --- dangling geometry references ---------------------------------------
        dangling = [c.tag for c in source.components
                    if c.geometry_key and c.geometry_key not in source.geometry]
        if dangling:
            warnings.append({
                "rule_code": "MISSING_GEOMETRY",
                "severity": "ERROR",
                "message": f"{len(dangling)} component(s) reference geometry that is "
                           f"absent from the container; they would be invisible in "
                           f"the viewer.",
                "sample": dangling[:20],
            })

        # --- orphaned hierarchy ---------------------------------------------------
        tags = {c.tag for c in source.components}
        orphans = [c.tag for c in source.components
                   if c.parent_tag and c.parent_tag not in tags]
        if orphans:
            warnings.append({
                "rule_code": "ORPHAN_COMPONENT",
                "severity": "WARN",
                "message": f"{len(orphans)} component(s) reference a parent that is not "
                           f"in this model; they will appear at the root of the tree.",
                "sample": orphans[:20],
            })

        # --- attribute coverage -----------------------------------------------------
        required_attrs = rules["required_attributes"]
        physical = [c for c in source.components if c.geometry_key]
        coverage: dict[str, float] = {}
        for attr in required_attrs:
            present = sum(1 for c in physical if c.attributes.get(attr))
            coverage[attr] = round(100.0 * present / len(physical), 1) if physical else 100.0

        ctx.state["validation_warnings"] = warnings
        ctx.state["attribute_coverage"] = coverage
        ctx.state["duplicate_tag_count"] = len(duplicates)

        for w in warnings:
            ctx.log(f"[{w['severity']}] {w['rule_code']}: {w['message']}",
                    level=w["severity"], stage=self.name)

        return {
            "components": len(source.components),
            "warnings": len(warnings),
            "duplicate_tags": len(duplicates),
            "bad_format_tags": len(bad_tags),
            "orphans": len(orphans),
            "dangling_geometry": len(dangling),
            "attribute_coverage_pct": coverage,
            "unit_system": units,
        }
