# TakeShape quote intake service rubrics

This is the editable service guide for the new text-only quote-intake agent. It covers the 13 categories in the current service catalog. It is separate from `SERVICE_RUBRICS.md` and `app/flow/service_rubrics.py`, which belong to the existing agent. These starter rules are intended for Noah and his partner to refine with provider feedback; they define an initial quote request, not a promise that every service can be priced without inspection.

## How to edit

Each service has one YAML block. Change its questions, priorities, and provider-review notes here; do not edit the global system prompt to add a service-specific question. The new backend must load this document directly. It must not require a second handwritten Python registry.

`decisions` are homeowner choices that matter to the initial request: scope, design, taste, lifestyle, service level, timing, or logistics. Ask them only when applicable, unanswered, and material to this request. `priority` ranks the useful questions within a service; 1 is highest. `category` says why the homeowner owns the answer. `defer` lists acceptable unresolved answer statuses, not defaults to invent. `unknown_outcome` deterministically selects `ready`, `needs_provider_review`, or `consultation_required` when the homeowner explicitly cannot answer; do not leave that choice to model prose. The common scope/outcome fields also apply; the same answer can satisfy equivalent common and service fields without another question. `optional` entries can enrich an answer but never cause another turn by themselves. `provider_review` and its model/technical subcategories are for the provider, not a list of questions to ask the homeowner. An unasked field remains unasked, not an invented homeowner preference.

The chat never reads or measures the 3D model. Once the homeowner's decisions are sufficiently clear, the provider manually reviews the model for dimensions, quantities, layout, counts, visible features, and other price inputs. Technical suitability, concealed conditions, diagnosis, code, and feasibility remain provider assessments. The homeowner should not be asked to reproduce model measurements or make technical conclusions. An explicit inclusive scope such as “everything,” “all windows,” or “the whole room” is a valid homeowner decision.

Questions are examples, not required verbatim wording. They must adapt to what the homeowner already said. No service entry may override the global no-measurement, no-visual-claims, no-invented-facts, or privacy rules. A conditional decision uses a structured `when` condition based on another field, with an optional validated `and_field`/`and_equals` pair when two known choices determine applicability; the loader must validate referenced fields and allowed values rather than execute a text expression.

Adding a service here does not automatically add it to provider routing. A non-null `catalog_service` must exactly match the supported catalog. Use `null` for an unmapped service and have the application mark it for human routing review. Test edits before deploying or explicitly reloading the document. Pin a validated document version and content hash to each new intake; preserve the snapshot for conversations already underway.

## Common rules

```yaml
rubric_version: quote-intake-v1.14
common:
  required:
    - id: desired_outcome
      question: "What would you like changed or taken care of?"
      rule: "Usually extract this from the opening request; ask only if unclear."
      category: scope
      unknown_outcome: consultation_required
    - id: target_scope
      question: "Which rooms, surfaces, or items should the request include?"
      rule: "Ask about intended inclusions or exclusions, never a physical inventory."
      category: scope
      unknown_outcome: consultation_required
  shared_optional:
    - "A meaningful deadline or scheduling restriction; do not ask automatically."
    - "Budget only when volunteered or necessary to distinguish substantially different requested scopes."
    - "Known symptoms or history that materially affect the requested work."
  provider_review:
    - "Measurements, quantities, geometry, visible condition, and capture coverage."
    - "Material identification and concealed, structural, mechanical, or code-related conditions."
    - "Trade-specific methods, exact specifications, permits, and price."
  provider_model_review:
    - "Manually measure model geometry, areas, lengths, openings, counts, layout, and visible features needed for pricing."
    - "Confirm capture coverage before relying on a model-derived quantity."
  provider_technical_review:
    - "Assess condition, materials, preparation, compatibility, concealed damage, structural or mechanical feasibility, code, and permits."
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
    category: scope
    unknown_outcome: consultation_required
    defer: []
  - id: extent
    priority: 2
    question: "Is this a full repaint or touch-ups in specific places?"
    category: service_level
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation]
  - id: finish_preference
    priority: 3
    question: "Do you have a finish in mind, or would you like the painter to recommend one?"
    category: preference
    unknown_outcome: needs_provider_review
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
    category: scope
    unknown_outcome: consultation_required
    values: [replace, repair, refinish]
    defer: [provider_recommendation]
  - id: preferred_flooring
    priority: 2
    when: {field: work_type, equals: replace}
    question: "Do you have a specific flooring product in mind, or would you like the provider to compare suitable options during the quote consultation?"
    category: preference
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: removal_included
    priority: 3
    when: {field: work_type, equals: replace}
    question: "Should the quote include removing and disposing of the existing floor?"
    category: scope
    unknown_outcome: consultation_required
    defer: [provider_recommendation, unknown]
  - id: detail_scope
    priority: 4
    when: {field: work_type, equals: replace}
    question: "Should the quote include stairs, applicable closets, and baseboard or shoe-molding work, or would you like those priced as options?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, unknown]
  - id: lifestyle_priorities
    priority: 5
    when: {field: work_type, equals: replace}
    question: "Are there pets, children, high-traffic areas, frequent spills or moisture, or low-maintenance priorities the provider should consider when comparing flooring options?"
    category: preference
    unknown_outcome: needs_provider_review
    defer: [no_preference, unknown]
  - id: moving_responsibility
    priority: 6
    when: {field: work_type, equals: replace}
    question: "Should the provider include moving furniture or appliances in the quote, or will those areas be cleared beforehand?"
    category: logistics
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, unknown]
optional:
  - "Known leaks, moisture history, radiant-floor heating, occupancy restrictions, timing, or material-storage needs, if volunteered."
provider_review:
  - "Recommend products and separately priced alternatives based on the homeowner's look, durability, comfort, maintenance, and budget priorities; do not treat one material category as universally longer-lasting or more scratch-resistant."
provider_model_review:
  - "Create a room-by-room takeoff of selected floor polygons, area, perimeter, openings, thresholds, visible transitions, stairs, fireplaces, cabinets, islands, vents, base/shoe trim, furniture, appliances, and capture gaps."
  - "Record apparent existing flooring and visible damage as observations, not confirmed material or concealed condition."
provider_technical_review:
  - "Verify hidden flooring layers, adhesive, subfloor type and condition, flatness, movement, moisture, concealed damage, product compatibility, acclimation, installation method, and required testing or allowances."
quote_options:
  - "Solid-hardwood product and installation option when suitable."
  - "Engineered-wood and/or LVP alternatives when they better match stated lifestyle or maintenance priorities."
  - "Separate stairs, trim, transitions, leveling, mitigation, and concealed-repair allowances when unresolved."
```

## Interior Remodeling

```yaml
id: interior_remodeling
catalog_service: Interior Remodeling
consultation_before_quote: true
decisions:
  - id: intended_changes
    priority: 1
    question: "What are the main changes you want to make in the space?"
    category: scope
    unknown_outcome: consultation_required
    defer: [provider_recommendation]
  - id: layout_intent
    priority: 2
    question: "Do you want to keep the current layout, or change how the space is arranged?"
    category: design
    unknown_outcome: consultation_required
    defer: [provider_recommendation, unknown]
  - id: design_direction
    priority: 3
    question: "Do you already have a design direction, or should the provider help with that?"
    category: design
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: keep_replace_priorities
    priority: 4
    question: "Are there any cabinets, fixtures, appliances, finishes, or features you definitely want to keep or replace?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: selection_stage
    priority: 5
    question: "Have you selected any materials, fixtures, or appliances yet, or should the provider help compare options?"
    category: design
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: occupancy_constraints
    priority: 6
    question: "Does the space need to remain usable during the work, or are there any phasing or access constraints the quote should account for?"
    category: logistics
    unknown_outcome: needs_provider_review
    defer: [not_applicable, no_preference, unknown]
  - id: listing_timeline
    priority: 7
    question: "Is there a target listing or completion date the provider should plan around, or is the timing flexible?"
    category: timing
    unknown_outcome: needs_provider_review
    defer: [unknown]
  - id: resale_investment_direction
    priority: 8
    question: "For the resale refresh, would you prefer a cost-conscious scope, a more premium finish, or options at both levels?"
    category: preference
    unknown_outcome: needs_provider_review
    values: [cost_conscious, premium, compare_options]
    defer: [provider_recommendation, no_preference, unknown]
optional:
  - "Keep versus replace decisions already named by the homeowner."
  - "A stated budget ceiling or need to keep part of the space usable."
provider_review:
  - "Dimensions, structural feasibility, systems relocation, permits, and whether design/assessment is needed before pricing."
  - "For cabinet refreshes, verify that doors, boxes, and hinge geometry are suitable for repainting and concealed soft-close hardware."
  - "For farmhouse sinks, verify cabinet modification/support, countertop fabrication, plumbing, disposal, and finish repair requirements."
provider_technical_review:
  - "Confirm countertop removal and substrate/cabinet support, sink and faucet compatibility, plumbing changes, backsplash preparation, coating preparation, ventilation, permits, and concealed water damage."
quote_options:
  - "For resale work, provide cost-conscious and upgraded finish options when the homeowner requests a comparison; identify allowances for unselected slab, tile, sink, paint, and hardware products."
  - "Separate known cosmetic scope from cabinet, plumbing, countertop, or concealed-condition allowances that require consultation or field verification."
```

## Windows and Doors

```yaml
id: windows_doors
catalog_service: Window & Door Install
decisions:
  - id: subtype
    priority: 1
    question: "Is the work for windows, doors, or both?"
    category: routing
    unknown_outcome: consultation_required
    values: [windows, doors, both]
    defer: []
  - id: work_type
    priority: 2
    question: "Are you replacing existing ones, repairing them, or adding a new opening?"
    category: scope
    unknown_outcome: consultation_required
    values: [replace, repair, new_opening]
    defer: [provider_recommendation]
  - id: product_preference
    priority: 3
    when: {field: work_type, one_of: [replace, new_opening]}
    question: "Have you chosen a product, or would you like the provider to recommend options?"
    category: preference
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: operation_and_grid_preferences
    priority: 4
    when: {field: work_type, one_of: [replace, new_opening]}
    question: "Do you have preferences for grids and how the windows should open, including any bay, picture, or specialty windows?"
    category: design
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: pricing_structure
    priority: 5
    when: {field: work_type, equals: replace}
    question: "Should the provider price the project as one scope or as separate phases or option groups?"
    category: scope
    unknown_outcome: needs_provider_review
    required_for_quote: false
    defer: [not_applicable, provider_recommendation, no_preference, unknown]
  - id: supply_install_scope
    priority: 6
    when: {field: work_type, one_of: [replace, new_opening]}
    question: "Should the provider supply and install the products, or are you purchasing them separately?"
    category: logistics
    unknown_outcome: needs_provider_review
    values: [provider_supply_and_install, homeowner_supplied, undecided]
    defer: [provider_recommendation, unknown]
  - id: performance_priorities
    priority: 7
    when: {field: work_type, one_of: [replace, new_opening]}
    question: "Are energy efficiency, noise reduction, security, maintenance, or another performance goal especially important?"
    category: preference
    unknown_outcome: needs_provider_review
    defer: [no_preference, unknown]
  - id: trim_finish_scope
    priority: 8
    when: {field: work_type, one_of: [replace, new_opening]}
    question: "Should existing trim be reused where possible, replaced to match its current look, or changed to a new style?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, unknown]
  - id: known_opening_damage
    priority: 9
    question: "Do you know of any leaks, rot, drafts, sticking, or other damage around the openings? It is okay if you are not sure."
    category: service_level
    unknown_outcome: needs_provider_review
    defer: [unknown]
optional:
  - "Whether the homeowner intends supply-and-install or installation of a product they already own."
  - "Operational symptoms if repair is requested; do not repeat symptoms already described."
provider_review:
  - "Opening sizes and counts, frame condition, fit, structural changes, and product suitability."
quote_options:
  - "When phased or option-group pricing is requested, preserve each requested group separately rather than converting later phases into a future request."
  - "For airport, road, or other exterior noise concerns, compare suitable product configurations using provider/manufacturer performance data and state which window groups receive the upgrade."
```

## Handyman

```yaml
id: handyman
catalog_service: Handyman
decisions:
  - id: task_and_result
    priority: 1
    question: "What would you like repaired, installed, or taken care of?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: []
  - id: supply_responsibility
    priority: 2
    question: "Do you already have the item or parts, or should the provider supply them?"
    category: logistics
    unknown_outcome: needs_provider_review
    defer: [not_applicable, provider_recommendation, unknown]
  - id: completion_details
    priority: 3
    question: "Should the quote include finishing work such as paint touch-up, caulk, debris removal, or concealing wires where applicable?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [not_applicable, provider_recommendation, no_preference, unknown]
  - id: task_specific_details
    priority: 4
    question: "What task-specific details could affect the work—for example the mounting surface and wire-routing preference, drywall-hole location and finish, or whether matching paint is available?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, unknown]
optional:
  - "A short list of additional tasks already requested by the homeowner; do not upsell."
  - "For trim repair, assume the replacement should match the existing profile and finish unless the homeowner asks for a change."
provider_review:
  - "Dimensions, mounting substrate, compatibility, safe/code-compliant wire routing, and whether a licensed specialist is required."
```

## Interior Cleaning

```yaml
id: interior_cleaning
catalog_service: Interior Cleaning
decisions:
  - id: cleaning_goal
    priority: 1
    question: "Is this regular cleaning, a deeper clean, or a move-in or move-out clean?"
    category: service_level
    unknown_outcome: needs_provider_review
    values: [regular, deep, move_in, move_out, post_project]
    defer: [provider_recommendation]
  - id: frequency
    priority: 2
    when: {field: cleaning_goal, one_of: [regular, deep]}
    question: "Is this a one-time clean or something you'd like regularly?"
    category: service_level
    unknown_outcome: needs_provider_review
    defer: [unknown]
  - id: cleaning_extras
    priority: 3
    question: "Should the quote include extras such as inside the oven, refrigerator, cabinets, or interior windows?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: cleaning_condition_factors
    priority: 4
    question: "Is there heavy buildup, pet hair, smoke residue, construction dust, or another condition the cleaner should plan for?"
    category: service_level
    unknown_outcome: needs_provider_review
    defer: [not_applicable, unknown]
  - id: cleaning_logistics
    priority: 5
    question: "Do you have a target date, and are there any entry, pet, fragrance, product, or delicate-surface instructions the cleaner should plan for?"
    category: logistics
    unknown_outcome: needs_provider_review
    defer: [not_applicable, no_preference, unknown]
optional:
  - "Requested extras such as inside appliances, when volunteered."
  - "A whole-house request includes all rooms unless the homeowner explicitly excludes one; do not ask for exclusions automatically."
provider_review:
  - "Area, visible condition, staffing/time allowance, and any work outside routine cleaning."
quote_options:
  - "One-time initial cleaning, assumed deep clean when the homeowner is unsure."
  - "Routine/recurring cleaning, assumed regular cleaning when the homeowner is unsure."
```

## Decking

```yaml
id: decking
catalog_service: Decking
decisions:
  - id: work_type
    priority: 1
    question: "Are you repairing a deck, replacing it, refinishing it, or planning a new one?"
    category: scope
    unknown_outcome: consultation_required
    values: [repair, replace, refinish, new]
    defer: [provider_recommendation]
  - id: intended_result
    priority: 2
    when: {field: work_type, one_of: [repair, refinish]}
    question: "What would you like the finished deck to be like?"
    category: design
    unknown_outcome: consultation_required
    defer: [provider_recommendation, unknown]
  - id: deck_layout_scope
    priority: 3
    when: {field: work_type, one_of: [replace, new]}
    question: "Should the deck keep the current size and layout, or should the quote include changes or an expansion?"
    category: design
    unknown_outcome: consultation_required
    defer: [provider_recommendation, unknown]
  - id: deck_material
    priority: 4
    when: {field: work_type, one_of: [replace, new]}
    question: "Do you prefer wood, composite, PVC, or would you like the provider to compare options?"
    category: preference
    unknown_outcome: needs_provider_review
    values: [wood, composite, pvc]
    defer: [provider_recommendation, no_preference, unknown]
  - id: deck_components
    priority: 5
    when: {field: work_type, one_of: [repair, replace, new]}
    question: "Should the quote include railings, stairs, skirting, lighting, or other deck features?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: deck_removal
    priority: 6
    when: {field: work_type, equals: replace}
    question: "Should removal and disposal of the existing deck be included?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, unknown]
  - id: deck_known_issues
    priority: 7
    when: {field: work_type, one_of: [repair, replace]}
    question: "Do you know of any loose railings, soft boards, settling, or other structural concerns? It is okay if you are not sure."
    category: service_level
    unknown_outcome: needs_provider_review
    defer: [unknown]
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
    category: routing
    unknown_outcome: consultation_required
    values: [roofing, siding, gutter_installation, multiple]
    defer: []
  - id: work_type
    priority: 2
    question: "Are you looking for a repair, replacement, or help identifying a problem?"
    category: scope
    unknown_outcome: consultation_required
    values: [repair, replace, assess]
    defer: [provider_recommendation]
  - id: intended_result
    priority: 3
    when: {field: work_type, equals: replace}
    question: "Do you want a similar replacement, a different material, or a provider recommendation?"
    category: preference
    unknown_outcome: needs_provider_review
    defer: [not_applicable, provider_recommendation, no_preference, unknown]
  - id: siding_coverage
    priority: 4
    when: {field: subtype, equals: siding, and_field: work_type, and_equals: replace}
    question: "Should the new siding cover the entire house or only certain sides or sections, and are any attached or detached structures included?"
    category: scope
    unknown_outcome: consultation_required
    defer: []
  - id: siding_trim_scope
    priority: 5
    when: {field: subtype, equals: siding, and_field: work_type, and_equals: replace}
    question: "Should the quote include matching exterior trim, soffit, or fascia work, or siding only?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, unknown]
  - id: siding_finish_direction
    priority: 6
    when: {field: subtype, equals: siding, and_field: work_type, and_equals: replace}
    question: "Do you have a color and siding style in mind, or would you like the provider to show options?"
    category: preference
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: siding_known_issues
    priority: 7
    when: {field: subtype, equals: siding, and_field: work_type, and_equals: replace}
    question: "Do you know of any leaks, loose siding, storm damage, or visible rot that should be accounted for? It is okay if you are not sure."
    category: service_level
    unknown_outcome: needs_provider_review
    defer: [unknown]
  - id: siding_removal
    priority: 8
    when: {field: subtype, equals: siding, and_field: work_type, and_equals: replace}
    question: "Should removal and disposal of the existing siding be included?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, unknown]
  - id: roof_coverage
    priority: 4
    when: {field: subtype, equals: roofing, and_field: work_type, and_equals: replace}
    question: "Should the replacement cover the entire main house roof, and are garages, porches, additions, or other roof sections included?"
    category: scope
    unknown_outcome: consultation_required
    defer: []
  - id: roof_material_direction
    priority: 5
    when: {field: subtype, equals: roofing, and_field: work_type, and_equals: replace}
    question: "Do you have a roofing material, style, or color in mind, or should the provider compare options?"
    category: preference
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: roof_tearoff
    priority: 6
    when: {field: subtype, equals: roofing, and_field: work_type, and_equals: replace}
    question: "Should the quote include removing and disposing of the existing roofing?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, unknown]
  - id: roof_components
    priority: 7
    when: {field: subtype, equals: roofing, and_field: work_type, and_equals: replace}
    question: "Should related items such as skylights, vents, flashing, or gutters be included or priced separately?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: roof_known_issues
    priority: 8
    when: {field: subtype, equals: roofing, and_field: work_type, and_equals: replace}
    question: "Are there known leaks, storm damage, ventilation concerns, or prior inspection findings? It is okay if you are not sure."
    category: service_level
    unknown_outcome: needs_provider_review
    defer: [unknown]
  - id: gutter_downspouts
    priority: 10
    when: {field: subtype, equals: gutter_installation, and_field: work_type, and_equals: replace}
    question: "A whole-house gutter replacement normally includes matching new downspouts. Should the quote include those too?"
    category: scope
    unknown_outcome: needs_provider_review
    values: [replace_with_matching, keep_existing, downspouts_only]
    defer: [provider_recommendation, unknown]
  - id: gutter_discharge
    priority: 11
    when: {field: subtype, equals: gutter_installation, and_field: work_type, and_equals: replace}
    question: "Do the downspouts drain through above-ground extensions or splash blocks, connect to underground drainage, or use a mix? It is okay if you are not sure."
    category: scope
    unknown_outcome: needs_provider_review
    values: [above_ground, underground, mixed]
    defer: [unknown]
  - id: gutter_guards
    priority: 12
    when: {field: subtype, equals: gutter_installation, and_field: work_type, and_equals: replace}
    question: "Should gutter guards be included, excluded, or priced as an option?"
    category: preference
    unknown_outcome: needs_provider_review
    values: [include, exclude, price_as_option]
    defer: [provider_recommendation, no_preference, unknown]
  - id: gutter_drainage_history
    priority: 13
    when: {field: subtype, equals: gutter_installation, and_field: work_type, and_equals: replace}
    question: "Have you noticed overflowing gutters, pooling near the foundation, or water spilling in any particular areas? It is okay if you are not sure."
    category: service_level
    unknown_outcome: needs_provider_review
    defer: [unknown]
  - id: gutter_structures
    priority: 14
    when: {field: subtype, equals: gutter_installation, and_field: work_type, and_equals: replace}
    question: "Should this include only the main house, or also any detached garage, porch, or addition?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [unknown]
  - id: gutter_visible_fascia
    priority: 15
    when: {field: subtype, equals: gutter_installation, and_field: work_type, and_equals: replace}
    question: "Do you know of any visible fascia or soffit damage that may need repair? It is okay if you are not sure."
    category: scope
    unknown_outcome: needs_provider_review
    defer: [unknown]
optional:
  - "Known leak/storm history or prior inspection, if relevant and not already described."
provider_review:
  - "Roof or elevation coverage, dimensions, pitch, materials, cause of a leak, and concealed condition."
  - "Never infer exterior coverage from an interior scan. Never ask the homeowner to climb or inspect a roof."
  - "For gutters, verify run-by-run takeoff, corners, outlets, matching downspout count/routes, roof drainage areas, mounting height, fascia/soffit/drip-edge condition, access, and underground connection condition."
provider_technical_review:
  - "For gutter installation, confirm capacity and downspout sizing from roof area, pitch, valleys, and local design rainfall; confirm hanger method, material compatibility, permits, hidden rot, and whether underground drainage is usable."
quote_options:
  - "For whole-house gutter replacement, include matching new downspouts by default unless the homeowner explicitly excludes them."
  - "State removal and disposal, seamless or sectional construction, material/profile/size/color, hangers, extensions or splash blocks, underground adapters, fascia-repair allowance, cleanup, and water-flow testing."
  - "Offer gutter guards separately when the homeowner asks to have them priced as an option."
```

## Window Cleaning

```yaml
id: window_cleaning
catalog_service: Window Cleaning
decisions:
  - id: sides_included
    priority: 1
    question: "Would you like the inside, outside, or both cleaned?"
    category: scope
    unknown_outcome: needs_provider_review
    values: [inside, outside, both]
    defer: []
  - id: extras
    priority: 2
    question: "Should the quote include screens and tracks, or glass only?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: window_type_details
    priority: 3
    question: "Do you know whether any are storm windows, French or exterior-grid panes, skylights, or other specialty glass? It is okay if you are not sure."
    category: service_level
    unknown_outcome: needs_provider_review
    defer: [unknown]
  - id: cleaning_condition
    priority: 4
    question: "Is this routine cleaning, or is there hard-water staining, paint, adhesive, or construction debris to remove? It is okay if you are not sure."
    category: service_level
    unknown_outcome: needs_provider_review
    defer: [unknown]
  - id: interior_access
    priority: 5
    when: {field: sides_included, one_of: [inside, both]}
    question: "Are any interior glass surfaces difficult to reach, such as lofted windows in a vaulted room? It is okay if you are not sure."
    category: logistics
    unknown_outcome: needs_provider_review
    defer: [unknown]
optional:
  - "A recurring schedule or requested removal of a particular substance, if volunteered."
provider_review:
  - "Window counts, dimensions, exterior height/access, visible features, and capture coverage from the model; do not ask the homeowner for the home's story count."
quote_options:
  - "Exterior glass cleaning."
  - "Interior glass cleaning."
  - "Screen detailing."
  - "Sill and track detailing."
```

## Gutter Cleaning

```yaml
id: gutter_cleaning
catalog_service: Gutter Cleaning
decisions:
  - id: cleaning_goal
    priority: 1
    question: "Is this routine cleaning, or are you having an overflow or drainage problem?"
    category: service_level
    unknown_outcome: ready
    defer: [unknown]
  - id: gutter_guard_access
    priority: 2
    question: "Do you have gutter guards now, or would you like gutter guard installation included as an option? It is okay if you are not sure what is installed."
    category: scope
    unknown_outcome: needs_provider_review
    defer: [not_applicable, no_preference, unknown]
  - id: gutter_brightening
    priority: 3
    question: "Would you like exterior gutter whitening or brightening priced as an optional add-on, or cleaning only?"
    category: preference
    unknown_outcome: needs_provider_review
    defer: [not_applicable, no_preference, unknown]
  - id: gutter_structures_and_damage
    priority: 4
    question: "Should detached garages, additions, or other structures be included, and are there any known loose, leaking, or damaged gutter sections?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [not_applicable, unknown]
optional:
  - "Bagging and hauling gutter debris is not standard; include and price it only when the homeowner requests it."
  - "Requested recurring maintenance, gutter guard installation, or exterior brightening."
provider_review:
  - "Standard cleaning removes debris from the gutter channels by a provider-selected safe method and clears resulting debris from outdoor furniture, walkways, patios, and the driveway; it does not include bagging and hauling unless requested."
  - "Verify lengths, height, guards, access, downspout blockages, overflow cause, and whether repairs or underground-drain work need separate pricing."
quote_options:
  - "Separately price requested gutter guard installation and exterior gutter whitening or brightening."
```

## Power Washing

```yaml
id: power_washing
catalog_service: Power Washing
decisions:
  - id: service_branch
    priority: 1
    question: "Are you looking to wash the house exterior, concrete or other flatwork, or both?"
    category: routing
    unknown_outcome: needs_provider_review
    values: [soft_wash, flatwork, mixed]
    defer: [unknown]
  - id: intended_surfaces
    priority: 2
    question: "Which surfaces would you like cleaned, and is anything off-limits?"
    category: scope
    unknown_outcome: consultation_required
    defer: []
  - id: optional_addons
    priority: 3
    when: {field: service_branch, one_of: [soft_wash, mixed]}
    question: "The standard whole-house wash includes all siding/elevations, exterior gutter faces, soffits, and fascia. Often, homeowners like to have patios or porches, roof washing, or detailed exterior window cleaning done along with the house wash. Would you like any of those quoted as well?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [not_applicable, no_preference, unknown]
  - id: cleaning_level
    priority: 4
    question: "Is this standard cleaning for dirt, algae, mold, or mildew, or specialty stain/restoration cleaning for rust, red clay, efflorescence, oxidation, paint, or construction residue?"
    category: service_level
    unknown_outcome: needs_provider_review
    values: [standard_cleaning, specialty_restoration, mixed_or_unsure]
    defer: [unknown]
  - id: added_surface_material
    priority: 5
    when: {field: service_branch, one_of: [flatwork, mixed]}
    question: "What are the included patios, porches, walkways, or other hard surfaces made of—concrete, pavers, wood, composite, or something else? It is okay if you are not sure."
    category: service_level
    unknown_outcome: needs_provider_review
    defer: [unknown]
  - id: concrete_finish_request
    priority: 6
    when: {field: service_branch, one_of: [flatwork, mixed]}
    question: "If you want more than cleaning on eligible concrete, should sealing or staining be included?"
    category: preference
    unknown_outcome: needs_provider_review
    required_for_quote: false
    values: [seal, stain]
    defer: [not_applicable, no_preference, unknown]
optional:
  - "A stated water-access or scheduling restriction."
  - "Known loose, damaged, oxidized, freshly painted, or leaking areas, when volunteered; the provider verifies surface condition and method."
provider_review:
  - "Surface areas and materials, condition, suitable pressure/chemistry, runoff, and access."
  - "A whole-house soft-wash request includes all siding/elevations, exterior gutter faces, soffits, and fascia unless the homeowner excludes an area. It does not include interior gutter clearing."
  - "Detailed exterior window cleaning is a separately priced add-on with a dedicated glass finish; an incidental rinse during house washing is not finished window cleaning."
  - "For explicitly requested concrete sealing or staining, assess preparation, compatibility, coverage, drying, and product suitability."
quote_options:
  - "Whole-house soft wash: siding/elevations, exterior gutter faces, soffits, and fascia."
  - "Separately price requested patios/porches, roof washing, detailed exterior window cleaning, and eligible flatwork finish treatments."
```

## Moving

```yaml
id: moving
catalog_service: Moving
decisions:
  - id: move_scope
    priority: 1
    question: "Are you moving everything in the scanned space or only certain items?"
    category: scope
    unknown_outcome: consultation_required
    defer: []
  - id: destination_area
    priority: 2
    question: "What city or ZIP are you moving to? No street address needed yet."
    category: logistics
    unknown_outcome: needs_provider_review
    defer: [unknown]
  - id: timing
    priority: 3
    question: "Do you have a moving date, or is your timing flexible?"
    category: timing
    unknown_outcome: needs_provider_review
    defer: [unknown]
  - id: help_needed
    priority: 4
    question: "Do you need packing help too, or just moving the items?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, unknown]
  - id: move_access
    priority: 5
    question: "Are there stairs, elevators, long carries, narrow access, parking restrictions, or building rules at either location? It is okay if you are not sure."
    category: logistics
    unknown_outcome: needs_provider_review
    defer: [unknown]
  - id: special_items
    priority: 6
    question: "Are there pianos, safes, oversized furniture, fragile items, or anything else needing special handling?"
    category: logistics
    unknown_outcome: needs_provider_review
    defer: [not_applicable, unknown]
  - id: moving_services
    priority: 7
    question: "Should the quote include disassembly, reassembly, packing materials, storage, or disposal in addition to the move?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [not_applicable, provider_recommendation, no_preference, unknown]
  - id: model_inventory_coverage
    priority: 8
    question: "Have all rooms and storage areas with items being moved—including the garage, closets, basement, and attic—been captured in the app? If an attic is unfinished or unsafe to walk, photos taken from a safe access point are enough."
    category: logistics
    unknown_outcome: needs_provider_review
    defer: [unknown]
optional:
  - "Unscanned or off-site items, approximate packed-box volume, extra stops, and valuation/protection preferences."
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
    category: scope
    unknown_outcome: needs_provider_review
    defer: []
  - id: removal_location
    priority: 2
    question: "Will the items be set out, or should the crew remove them from where they are?"
    category: logistics
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, unknown]
  - id: special_disposal
    priority: 3
    question: "Does the load include paint, chemicals, batteries, tires, refrigerants, construction debris, or anything else that may need special disposal?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [not_applicable, unknown]
  - id: heavy_disassembly
    priority: 4
    question: "Does anything require disassembly or unusually heavy handling, such as a treadmill, safe, hot tub, or built-in item?"
    category: logistics
    unknown_outcome: needs_provider_review
    defer: [not_applicable, unknown]
  - id: removal_preferences
    priority: 5
    question: "Do you have a removal deadline or preferences for donation, recycling, or a final sweep-up?"
    category: logistics
    unknown_outcome: needs_provider_review
    defer: [not_applicable, no_preference, unknown]
optional:
  - "Provider acceptance of regulated or restricted materials must be confirmed; never imply paint, batteries, refrigerants, chemicals, or similar items are automatically accepted."
provider_review:
  - "Volume, dimensions, weight, access, disposal eligibility, and any special handling."
```

## Landscaping

```yaml
id: landscaping
catalog_service: Landscaping
consultation_when: {field: work_category, one_of: [landscape_design, outdoor_construction]}
decisions:
  - id: work_category
    priority: 1
    question: "Is this maintenance, cleanup, planting, lawn treatment, sod, landscape design, or outdoor construction?"
    category: routing
    unknown_outcome: consultation_required
    values: [maintenance, cleanup, planting, lawn_treatment, sod, landscape_design, outdoor_construction]
    defer: []
  - id: requested_work
    priority: 2
    question: "What would you like done in the yard?"
    category: scope
    unknown_outcome: consultation_required
    defer: []
  - id: target_areas
    priority: 3
    question: "Which parts of the property should the work include?"
    category: scope
    unknown_outcome: consultation_required
    defer: []
  - id: design_preferences
    priority: 4
    when: {field: work_category, one_of: [planting, landscape_design, outdoor_construction]}
    question: "What style, materials, plants, or maintenance level do you prefer?"
    category: design
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: existing_landscape_scope
    priority: 5
    when: {field: work_category, one_of: [cleanup, planting, sod, landscape_design, outdoor_construction]}
    question: "Should the quote include removing existing plants, turf, beds, debris, or other landscape materials?"
    category: scope
    unknown_outcome: needs_provider_review
    defer: [not_applicable, provider_recommendation, unknown]
  - id: ongoing_maintenance
    priority: 6
    when: {field: work_category, one_of: [planting, lawn_treatment, sod, landscape_design]}
    question: "Should the provider plan for ongoing maintenance, irrigation needs, or a low-maintenance result after installation?"
    category: preference
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: landscape_materials_and_features
    priority: 7
    when: {field: work_category, one_of: [planting, sod, landscape_design, outdoor_construction]}
    question: "Do you prefer mulch, decorative stone, or a provider recommendation, and should existing edging or any plants and features be preserved?"
    category: design
    unknown_outcome: needs_provider_review
    defer: [provider_recommendation, no_preference, unknown]
  - id: landscape_site_and_timing
    priority: 8
    when: {field: work_category, one_of: [planting, sod, landscape_design, outdoor_construction]}
    question: "Are there known sun, shade, drainage, irrigation, or timing considerations the provider should plan around? It is okay if you are not sure."
    category: logistics
    unknown_outcome: needs_provider_review
    defer: [unknown]
optional:
  - "Lifestyle preferences such as pets, children, privacy, entertaining, irrigation, or low-maintenance planting."
  - "Requested frequency for mowing, trimming, fertilizing, spraying, or seasonal cleanup."
provider_review:
  - "Manually assess model/site size, layout, visible features, access, quantities, and material coverage."
  - "Assess soil, drainage, grading, plant health, utilities, feasibility, and construction requirements."
  - "Outdoor construction such as patios, decks, and outdoor living spaces requires consultation before quote."
```

## Unmapped service or assessment request

```yaml
id: generic_assessment
catalog_service: null
decisions:
  - id: desired_help
    priority: 1
    question: "What would you like the provider to help you change or figure out?"
    category: scope
    unknown_outcome: needs_provider_review
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
