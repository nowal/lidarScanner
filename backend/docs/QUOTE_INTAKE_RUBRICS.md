# TakeShape quote intake service rubrics

This is the editable service guide for the new text-only quote-intake agent. It covers the 13 categories in the current service catalog. It is separate from `SERVICE_RUBRICS.md` and `app/flow/service_rubrics.py`, which belong to the existing agent. These starter rules are intended for Noah and his partner to refine with provider feedback; they define an initial quote request, not a promise that every service can be priced without inspection.

## How to edit

Each service has one YAML block. Change its questions, priorities, and provider-review notes here; do not edit the global system prompt to add a service-specific question. The new backend must load this document directly. It must not require a second handwritten Python registry.

`decisions` are homeowner choices that matter to the initial request. Ask them only when applicable, unanswered, and material to this request. `priority` ranks the useful questions within a service; 1 is highest. `defer` lists acceptable unresolved outcomes, not defaults to invent. The common scope/outcome fields also apply; the same answer can satisfy equivalent common and service fields without another question. `optional` entries can enrich an answer but never cause another turn by themselves. `provider_review` is for the provider, not a list of questions to ask the homeowner. An empty list of `defer` outcomes means an unresolved core decision needs a clarifier or an explicitly labeled assessment scope, not repeated interrogation. An unasked field remains unasked, not an invented homeowner preference.

Questions are examples, not required verbatim wording. They must adapt to what the homeowner already said. No service entry may override the global no-measurement, no-visual-claims, no-invented-facts, or privacy rules. A conditional decision uses a structured `when` condition based on another field; the loader must validate the referenced field and allowed values rather than execute a text expression.

Adding a service here does not automatically add it to provider routing. A non-null `catalog_service` must exactly match the supported catalog. Use `null` for an unmapped service and have the application mark it for human routing review. Test edits before deploying or explicitly reloading the document. Pin a validated document version and content hash to each new intake; preserve the snapshot for conversations already underway.

## Common rules

```yaml
rubric_version: quote-intake-v1
common:
  required:
    - id: desired_outcome
      question: "What would you like changed or taken care of?"
      rule: "Usually extract this from the opening request; ask only if unclear."
    - id: target_scope
      question: "Which rooms, surfaces, or items should the request include?"
      rule: "Ask about intended inclusions or exclusions, never a physical inventory."
  shared_optional:
    - "A meaningful deadline or scheduling restriction; do not ask automatically."
    - "Budget only when volunteered or necessary to distinguish substantially different requested scopes."
    - "Known symptoms or history that materially affect the requested work."
  provider_review:
    - "Measurements, quantities, geometry, visible condition, and capture coverage."
    - "Material identification and concealed, structural, mechanical, or code-related conditions."
    - "Trade-specific methods, exact specifications, permits, and price."
  readiness:
    - "Service or explicit assessment need, intended target, and desired outcome are clear."
    - "Applicable homeowner decisions are answered or legitimately deferred."
    - "Unknown physical facts are labeled for provider review, never silently treated as satisfied."
    - "ZIP and upload readiness are application gates, not additional service questions."
```

## Painting

```yaml
id: painting
catalog_service: Painting
decisions:
  - id: included_surfaces
    priority: 1
    question: "Walls only, or should the quote include trim, ceilings, or other surfaces?"
    defer: []
  - id: extent
    priority: 2
    question: "Is this a full repaint or touch-ups in specific places?"
    defer: [provider_recommendation]
  - id: finish_preference
    priority: 3
    question: "Do you have a finish in mind, or would you like the painter to recommend one?"
    defer: [no_preference, provider_recommendation, unknown]
optional:
  - "Whether a requested repair or wallpaper removal should be included, if mentioned."
  - "Who moves belongings or supplies paint, if this is a stated constraint."
provider_review:
  - "Areas, trim lengths, visible preparation needs, substrate, access, and coating specification."
```

## Flooring

```yaml
id: flooring
catalog_service: Flooring
decisions:
  - id: work_type
    priority: 1
    question: "Are you looking to replace the floor, repair it, or refinish it?"
    values: [replace, repair, refinish]
    defer: [provider_recommendation]
  - id: preferred_flooring
    priority: 2
    when: {field: work_type, equals: replace}
    question: "Do you have a flooring material in mind, or would you like recommendations?"
    defer: [provider_recommendation, no_preference, unknown]
  - id: removal_included
    priority: 3
    when: {field: work_type, equals: replace}
    question: "Should the quote include removing and disposing of the existing floor?"
    defer: [provider_recommendation, unknown]
optional:
  - "Include transitions or baseboards if the homeowner requests them."
  - "Known moisture history or restrictions on work while the home is occupied."
provider_review:
  - "Floor area, existing material, leveling, subfloor, moisture, transitions, and concealed damage."
```

## Interior Remodeling

```yaml
id: interior_remodeling
catalog_service: Interior Remodeling
decisions:
  - id: intended_changes
    priority: 1
    question: "What are the main changes you want to make in the space?"
    defer: [provider_recommendation]
  - id: layout_intent
    priority: 2
    question: "Do you want to keep the current layout, or change how the space is arranged?"
    defer: [provider_recommendation, unknown]
  - id: design_direction
    priority: 3
    question: "Do you already have a design direction, or should the provider help with that?"
    defer: [provider_recommendation, no_preference, unknown]
optional:
  - "Keep versus replace decisions already named by the homeowner."
  - "A stated budget ceiling or need to keep part of the space usable."
provider_review:
  - "Dimensions, structural feasibility, systems relocation, permits, and whether design/assessment is needed before pricing."
```

## Windows and Doors

```yaml
id: windows_doors
catalog_service: Window & Door Install
decisions:
  - id: subtype
    priority: 1
    question: "Is the work for windows, doors, or both?"
    values: [windows, doors, both]
    defer: []
  - id: work_type
    priority: 2
    question: "Are you replacing existing ones, repairing them, or adding a new opening?"
    values: [replace, repair, new_opening]
    defer: [provider_recommendation]
  - id: product_preference
    priority: 3
    when: {field: work_type, one_of: [replace, new_opening]}
    question: "Have you chosen a product, or would you like the provider to recommend options?"
    defer: [provider_recommendation, no_preference, unknown]
optional:
  - "Whether the homeowner intends supply-and-install or installation of a product they already own."
  - "Operational symptoms if repair is requested; do not repeat symptoms already described."
provider_review:
  - "Opening sizes and counts, frame condition, fit, structural changes, and product suitability."
```

## Handyman

```yaml
id: handyman
catalog_service: Handyman
decisions:
  - id: task_and_result
    priority: 1
    question: "What would you like repaired, installed, or taken care of?"
    defer: []
  - id: supply_responsibility
    priority: 2
    question: "Do you already have the item or parts, or should the provider supply them?"
    defer: [not_applicable, provider_recommendation, unknown]
optional:
  - "A short list of additional tasks already requested by the homeowner; do not upsell."
provider_review:
  - "Dimensions, mounting substrate, compatibility, and whether a licensed specialist is required."
```

## Interior Cleaning

```yaml
id: interior_cleaning
catalog_service: Interior Cleaning
decisions:
  - id: cleaning_goal
    priority: 1
    question: "Is this regular cleaning, a deeper clean, or a move-in or move-out clean?"
    defer: [provider_recommendation]
  - id: frequency
    priority: 2
    question: "Is this a one-time clean or something you'd like regularly?"
    defer: [unknown]
optional:
  - "Requested extras such as inside appliances, when volunteered."
  - "Volunteered access, product, allergy, or pet-related constraints."
provider_review:
  - "Area, visible condition, staffing/time allowance, and any work outside routine cleaning."
```

## Decking

```yaml
id: decking
catalog_service: Decking
decisions:
  - id: work_type
    priority: 1
    question: "Are you repairing a deck, replacing it, refinishing it, or planning a new one?"
    defer: [provider_recommendation]
  - id: intended_result
    priority: 2
    question: "What would you like the finished deck to be like?"
    defer: [provider_recommendation, unknown]
optional:
  - "Material or finish preferences, if already chosen."
provider_review:
  - "Exterior coverage, dimensions, supports, structural condition, code, and coating suitability."
  - "A new deck may need design and site assessment before a construction quote."
```

## Roofing and Siding

```yaml
id: roofing_siding
catalog_service: Roofing & Siding
decisions:
  - id: subtype
    priority: 1
    question: "Is the work on the roof, siding, gutters, or more than one of those?"
    values: [roofing, siding, gutter_installation, multiple]
    defer: []
  - id: work_type
    priority: 2
    question: "Are you looking for a repair, replacement, or help identifying a problem?"
    values: [repair, replace, assess]
    defer: [provider_recommendation]
  - id: intended_result
    priority: 3
    when: {field: work_type, equals: replace}
    question: "Do you want a similar replacement, a different material, or a provider recommendation?"
    defer: [not_applicable, provider_recommendation, no_preference, unknown]
optional:
  - "Known leak/storm history or prior inspection, if relevant and not already described."
provider_review:
  - "Roof or elevation coverage, dimensions, pitch, materials, cause of a leak, and concealed condition."
  - "Never infer exterior coverage from an interior scan. Never ask the homeowner to climb or inspect a roof."
```

## Window Cleaning

```yaml
id: window_cleaning
catalog_service: Window Cleaning
decisions:
  - id: sides_included
    priority: 1
    question: "Would you like the inside, outside, or both cleaned?"
    defer: []
  - id: extras
    priority: 2
    question: "Should the quote include screens and tracks, or glass only?"
    defer: [provider_recommendation, no_preference, unknown]
optional:
  - "A recurring schedule or requested removal of a particular substance, if volunteered."
provider_review:
  - "Window counts, dimensions, height, access, condition, and capture coverage."
```

## Gutter Cleaning

```yaml
id: gutter_cleaning
catalog_service: Gutter Cleaning
decisions:
  - id: cleaning_goal
    priority: 1
    question: "Is this routine cleaning, or are you having an overflow or drainage problem?"
    defer: [unknown]
optional:
  - "Requested downspout flushing, debris disposal, or recurring maintenance."
provider_review:
  - "Lengths, height, guards, access, blockages, and whether a repair rather than cleaning is needed."
```

## Power Washing

```yaml
id: power_washing
catalog_service: Power Washing
decisions:
  - id: intended_surfaces
    priority: 1
    question: "Which surfaces would you like cleaned, and is anything off-limits?"
    defer: []
  - id: cleaning_goal
    priority: 2
    question: "Is this a general clean, or is there something specific you want removed?"
    defer: [no_preference, unknown]
optional:
  - "A stated water-access or scheduling restriction."
provider_review:
  - "Surface areas and materials, condition, suitable pressure/chemistry, runoff, and access."
```

## Moving

```yaml
id: moving
catalog_service: Moving
decisions:
  - id: move_scope
    priority: 1
    question: "Are you moving everything in the scanned space or only certain items?"
    defer: []
  - id: destination_area
    priority: 2
    question: "What city or ZIP are you moving to? No street address needed yet."
    defer: [unknown]
  - id: timing
    priority: 3
    question: "Do you have a moving date, or is your timing flexible?"
    defer: [unknown]
  - id: help_needed
    priority: 4
    question: "Do you need packing help too, or just moving the items?"
    defer: [provider_recommendation, unknown]
optional:
  - "Destination access restrictions or special handling the homeowner already knows about."
provider_review:
  - "Inventory/volume, sizes, origin access, destination conditions, and handling requirements."
  - "An unknown destination or date can require a provisional assessment rather than a firm moving price."
```

## Junk Removal

```yaml
id: junk_removal
catalog_service: Junk Removal
decisions:
  - id: removal_selection
    priority: 1
    question: "What should be removed, and what needs to stay?"
    defer: []
  - id: removal_location
    priority: 2
    question: "Will the items be set out, or should the crew remove them from where they are?"
    defer: [provider_recommendation, unknown]
optional:
  - "A requested donation destination, deadline, or known item requiring special disposal."
provider_review:
  - "Volume, dimensions, weight, access, disposal eligibility, and any special handling."
```

## Unmapped service or assessment request

```yaml
id: generic_assessment
catalog_service: null
decisions:
  - id: desired_help
    priority: 1
    question: "What would you like the provider to help you change or figure out?"
    defer: []
optional:
  - "Known symptoms and meaningful constraints already volunteered."
provider_review:
  - "Confirm the appropriate trade and whether an assessment is needed before pricing."
  - "Preserve the homeowner's service wording; do not claim catalog support or local availability."
```

## Example behavior checks

- “Paint the living room walls only, full repaint, you choose the finish, ZIP 01234.” All painting decisions and ZIP are supplied. Do not ask for dimensions, finish, or ZIP again.
- “Paint my living room.” Ask whether the intended scope includes walls only or other surfaces. Do not ask how large the room is.
- “Replace the bedroom carpet with LVP, including disposal.” Record replacement, preferred material, removal, and bedroom scope. Do not ask those questions again.
- “I don't know what finish.” Record unknown; record provider recommendation only if they request one. Do not rephrase the same finish question.
- “The roof leaks above this room.” Capture the symptom; clarify repair/assessment intent only if needed. Do not say an indoor model shows the roof or can diagnose the leak.
- “Clean every window, inside and out, with screens and tracks.” Ask for ZIP if missing; do not ask the total window count or repeat the included work.
- “Move everything here to 02139 next Friday; I'll pack.” All moving decisions are supplied. The destination ZIP does not establish the origin/project ZIP. The application must resolve the relative date using the user's time zone if it persists a calendar date.
- “Fix the plumbing.” Use the unmapped path unless the current catalog has explicitly added plumbing. Clarify the symptom or desired result, preserve the service name, and mark routing for review.

## Runtime validation and rollout

See [QUOTE_INTAKE_IMPLEMENTATION.md](QUOTE_INTAKE_IMPLEMENTATION.md#editing-the-service-guide)
for exact validation commands, snapshot behavior and deployment steps. The loader
reads this document directly; deployment plus a worker restart loads edits for
new intakes. Existing intakes keep their validated snapshot and hash.
