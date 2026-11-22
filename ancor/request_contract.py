"""Canonical request content fields shared by storage, tools, and presentation."""

FIELD_SPECS = (
    (
        'outcome',
        '目标',
        'Observable results the user needs. Every request requires at least one outcome point.',
    ),
    (
        'constraint',
        '约束',
        'Durable non-negotiable boundaries, including scope inclusions and exclusions, '
        'non-goals, prohibitions, compatibility, authority, quality limits, and deadlines.',
    ),
    (
        'advice',
        '建议',
        'Optional suggestions that preserve useful intent. Do not put required behavior here.',
    ),
    (
        'context',
        '背景',
        'Exact execution-disambiguation facts such as paths, inventories, schemas, '
        'environments, and current state. Context is not a commitment.',
    ),
)

FIELDS = tuple(field for field, _label, _description in FIELD_SPECS)
LABELS = {field: label for field, label, _description in FIELD_SPECS}
DESCRIPTIONS = {field: description for field, _label, description in FIELD_SPECS}
