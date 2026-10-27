# TakeShape AI Agent: Service Quote Rubrics

Quintin Lunsford's rubric document, received Oct 6, 2026, converted to Markdown without
changes to the wording. The runtime reads a condensed form of it from
`app/flow/service_rubrics.py`: the directives carry the rubric for the service in play,
the lead package prints the provider fields and the concealed-condition wording, and the
homeowner is told once, when quotes come back, what a quote covers.

Purpose: turn homeowner-selected LiDAR/RoomPlan scans, key frames and chat answers into a provider-ready scope for actual local service quotes. The agent gathers and explains evidence, obtains provider-issued prices, presents them to the homeowner and coordinates the chosen booking. The provider controls price, work method and acceptance of the scope.

This is a proposed agent knowledge and behavior specification for TakeShape. It reorganizes the researched trade rubric for the workflow described by the product owner; it does not mean an agent has been trained, integrated or validated in production. Service sources remain under each category. Apple sources support the capture boundaries; the routing and quote rules are TakeShape design recommendations.

## Capture boundaries and measurement rules

- **Indoor coverage** — Apple describes RoomPlan as an interior-room model and supports combining captured rooms. Record which rooms, levels and surfaces were actually scanned and which are requested for this job. Never call a partial scan a complete home survey or extrapolate missing areas as fact.
- **Exterior coverage** — Roofing, siding, gutters, exterior washing and landscaping require exterior photos/video, suitable measurements, plans or reports in addition to indoor RoomPlan. Use an exterior LiDAR/capture pipeline only when TakeShape actually supplies and validates that data; indoor dimensions are not a substitute.
- **Geometry versus interpretation** — Use structured geometry for supported dimensions and deterministic calculations; use key frames for visible details. Material, damage, subtype and condition inferred by vision remain observations until confirmed. A modeled furniture box does not reveal contents, weight or exact construction.
- **Confidence versus accuracy** — Apple’s surface confidence describes certainty in its category; it is not a dimensional tolerance or proof of quote accuracy. Track classification confidence separately from TakeShape measurement quality. Set trade-specific tolerances through validation/provider requirements; do not invent a universal centimeter or percentage guarantee.
- **Units and calculations** — Preserve native units, display provider units, document gross areas/opening deductions, and use suitable polygons for irregular geometry. Do not subtract openings twice from already-cut geometry. Record calculation inputs and uncertainty; the provider sets waste and installation allowances.
- **Identity and coverage** — Keep stable room, level, surface, opening and object IDs with supporting frame links. Reconcile overlapping scans, shared doors/windows and changed captures. One physical door should not double in an installation count, while two wall faces may both require paint.
- **Capture quality** — Check missing surfaces, occlusion, duplicate rooms, inconsistent dimensions and outdated frames. Ask for a targeted rescan, a close-up or a provider-specified measurement when it changes price. Show measured/derived values as such; a homeowner’s acceptance of a summary does not independently verify precision.
*Apple capture sources: RoomPlan overview · RoomPlan interior scope · RoomPlan multi-room capture · RoomPlan classification confidence*

## How the agent fills each service rubric

- **Select the service and job boundary first** — Identify the requested service/subtype, selected rooms/elevations/items, desired outcome and exclusions. A whole-home model may support a one-room job. Unscanned requested areas trigger follow-up; scanned unrequested areas do not automatically enter the scope.
- **Prefill, then ask only useful questions** — Populate supported quantities from the model and visible observations from frames. Ask about missing preferences, history, responsibilities and price-changing gaps. Do not ask homeowners to manually remeasure every supported dimension or repeat facts already supplied.
- **Required provider fields** — The list under each service defines the final information needed when applicable, not a questionnaire to read verbatim. Geometry/vision/chat supply different parts. For technical specifications, the homeowner may request a provider recommendation; obtain the provider’s selected specification or separately labeled options with its quote.
- **Classify unresolved fields** — Separate essential visible-scope inputs, conditional branch inputs, provider decisions and concealed conditions. An unknown quantity or requested finish normally needs clarification; a provider-priced product option may remain open until selection; possible concealed damage can remain disclosed under accepted terms.
- **Do not invent missing facts** — Use unknown or not applicable explicitly. Preserve conflicting readings and resolve material conflicts. A homeowner-reported fact, an AI inference and an independently verified measurement must never be silently merged into one certainty label.
- **Field record** — For each price-relevant value store field name, service/item/location ID, value and units, source type, evidence reference, coverage/occlusion, source time/version and verification status. Source types: scan geometry, calculated geometry, visual observation, homeowner report, external document or provider decision. Keep classification confidence and measurement-quality notes separate.
- **Safe collection** — Request evidence from normally accessible positions. For roofs, live electrical systems, structural diagnosis or technical testing, use existing records or qualified capture/assessment. The agent must not ask a homeowner to expose hazards to finish the rubric.
## The package every provider receives

- **Job summary** — Service address/area, property type, selected service and subtype, homeowner-approved inclusions/exclusions, requested timing, occupancy/access restrictions and a scope version tied to the model/frame set.
- **Quantities and evidence** — Readable room/item takeoff, units and measurement method; selected 3D views and annotated key frames; material/condition observations; homeowner confirmations and a clear list of unscanned/uncertain areas. Include only job-relevant capture content.
- **Scope decisions** — Desired products/finish or requested options, preparation and repairs, who supplies/moves/disconnects/removes items, utilities, delivery/equipment access, disposal, cleanup and completion standard. Include known leaks/damage and prior reports rather than hiding them inside a general caveat.
- **Concealed-condition terms** — List trade-specific unknowns, included allowances or exclusions, required pre-work checks, who performs them, any diagnostic/measurement fee, and the change-approval process. Apply the same scope and unknowns when seeking comparable provider quotes.
- **Provider response** — Ask for provider identity and service-area confirmation; quote ID and scope version; price and pricing basis; included work/options; taxes/fees, deposit/payment terms, validity, warranty, estimated duration and available appointment windows; assumptions/exclusions; and acceptance of the supplied measurements or specific verification still required.
- **Provider readiness decision** — Allow: quote supplied, targeted information requested, quote conditional on named checks, diagnostic/measurement service offered, or declined. Route questions back to the homeowner and refresh the shared scope if an answer changes it. A provider may apply its own acceptance standard.
## Concealed conditions: communicate to both parties

Possible hidden damage is a disclosed condition of many virtual quotes, not an automatic reason to force an estimating visit. Separate it from missing visible-scope information and from visible signs of an active problem. The provider decides whether it can price the visible scope with exclusions, allowances or unit rates.

Homeowner wording: “Your quote covers the work and conditions described in the TakeShape scope. The scan cannot show conditions beneath flooring, behind walls or inside other concealed areas. If the provider discovers an issue that changes the work, they will document it and explain any change in price or schedule for your approval before additional work proceeds. Any allowance or required check is listed in the quote.”

Provider wording: “This scope uses the attached scan measurements, key frames and homeowner answers, with their sources and limitations identified. Concealed conditions have not been verified. Please state what the base price includes, any allowances or unit rates, required checks and exclusions. Document newly discovered issues and obtain homeowner approval of price or schedule changes before additional work.”

- **Record agreement** — Show the applicable conditions with each quote before selection and record the accepted quote/scope version. A general acknowledgment does not authorize unspecified extra charges or turn an estimated quantity into a verified measurement.
- **Handle discovery** — Provider submits location-linked photos, the issue, proposed additional work, incremental cost, allowance credit if any and timing impact. The agent explains the request, obtains the homeowner’s decision and records the revised scope/price. Do not assume approval or quietly replace the accepted quote.
- **Known issue versus possibility** — “Subfloor not visible” may be a priced condition. “Visible water damage with unknown active leak” may require a provider question or diagnostic scope. Likewise, unresolved dimensions, unsafe access and missing design are not solved by a concealed-damage disclaimer.
## Retrieve, compare and book actual provider quotes

- **Request** — Identify actual local providers that serve the address and service. With the homeowner’s quote request, send the agreed package and track responses. Never label an AI-generated estimate or an unanswered request as an actual provider quote.
- **Present** — Show each provider’s price, scope, options, exclusions, allowances, required checks, timing, warranty and payment terms. Identify differences instead of implying all totals cover identical work. Preserve firm-price, hourly/unit-rate, conditional and budget-estimate distinctions.
- **Select and schedule** — Tie the homeowner’s choice to an exact provider, quote and scope version. Confirm availability, address, selected options, appointment type, agreed window and terms. Only report booked after provider/system confirmation; a request or tentative hold is not a confirmed booking.
- **Prerequisites** — If the provider requires final measurements or a diagnostic step, make clear whether the chosen appointment is that step or the service installation. Confirm product/order lead time where relevant. Reconfirm material changes in scope, price or schedule with the homeowner before committing.
## Reading the service sections

Each service retains its required provider fields, followed by six instructions: extract from scan; review key frames; ask the homeowner; collect missing evidence; send to provider; and apply the quote/hidden-condition rule. These instructions supplement the shared package and concealed-condition process above. They describe information requirements, not a promise that every provider will accept every project remotely.

## Painting

### Required provider fields

- **Scope and surfaces** — Interior or exterior; rooms, elevations, walls, ceilings, doors, cabinets, trim, and any excluded areas.
- **Measurements** — Wall length × height by room or elevation; ceiling area; linear feet and profile of baseboards, crown, casings, railings, and other trim.
- **Openings and details** — Count doors, windows, built-ins, stairwells, accent walls, high or vaulted ceilings, and complex cut-in edges.
- **Existing condition** — Peeling paint, holes, cracks, water stains, grease, mildew, wallpaper, texture, and bare or previously coated surfaces.
- **Preparation** — Cleaning, scraping, sanding, patching, caulking, priming, wallpaper removal, and whether repairs are included.
- **Finish specification** — Color changes, number of colors and coats, sheen, product grade, stain versus paint, and customer-supplied materials.
- **Protection and access** — Furniture, wall décor, fixtures, landscaping, flooring, occupied rooms, ladders or lifts, and who moves items.
- **Exterior specifics** — Siding material, height, sun/weather exposure, shutters, fascia/soffits, and substrate repairs.
- **Execution** — Work hours, drying/curing constraints, cleanup, waste, schedule, and final touch-up boundaries.
### TakeShape agent instructions

- **Extract from scan** — For selected rooms, produce wall-face IDs, lengths/heights, floor geometry and detected openings. Calculate gross wall area and report opening deductions separately. Derive ceiling area only when shape and coverage support it; floor area is not automatically ceiling or wall area. Keep both painted faces of shared walls distinct.
- **Review key frames** — Tag visible trim/profile, doors, cabinets, high/vaulted ceilings, wall décor, furniture, wallpaper, texture, holes, peeling and stains. Label material/condition interpretations as visual observations; a stain does not establish its cause.
- **Ask the homeowner** — Which rooms and surfaces? Walls, ceilings, trim, doors (which sides), cabinets or exterior? Confirm colors, sheen/product, coverage/coats or provider recommendation, preparation/repair standard, who moves/restores belongings, occupancy, timing and touch-up versus full-surface repainting. Specify if one side of a door/door frame needs painted or both sides.
- **Collect missing evidence** — Request close-ups with scale for repairs/trim, cabinet door/drawer counts and verified trim lengths where needed. Exterior work requires exterior dimensions and views of every included elevation; an indoor scan cannot supply them. Ask about known moisture, past repairs and property age where relevant to preparation.
- **Send to provider** — Room/elevation takeoff; gross/net area method; trim lengths and cabinet/door counts; annotated condition frames; finish and preparation choices; protection, access and cleanup responsibilities. Provider supplies coating system, labor assumptions and price.
- **Quote rule and concealed conditions** — Request a virtual quote once scope, usable dimensions, preparation and finish are resolved or priced as named options. Disclose possible hidden substrate/moisture issues and use the shared change process. A visible active problem or uncertain specialty coating needs provider review, rather than being silently covered by the caveat.
*Cross-check: CertaPro remote painting intake*

*Research: Sherwin-Williams paint calculator · Sherwin-Williams prep checklist*

## Roofing

### Required provider fields

- **Job type** — Repair, partial replacement, full replacement, or new roof; areas included and any detached structures.
- **Roof geometry** — Actual roof area by plane, pitch, stories/height, hips, valleys, dormers, and complicated intersections.
- **Existing system** — Material, age/condition, number of layers, leaks, storm damage, ventilation, and visible deck condition.
- **Tear-off and substrate** — Removal/disposal scope, underlayment, decking replacement allowance, and how concealed damage will be priced.
- **Edges and penetrations** — Ridge/hip, eaves, rakes, chimneys, skylights, vents, pipes, flashings, drip edge, and gutters.
- **Proposed assembly** — Shingle/metal/tile or low-slope system, grade/color, ice-and-water protection, ventilation, and warranty requirements.
- **Access and logistics** — Steep sections, landscaping, driveway or dumpster placement, material delivery, fall protection, and power-line obstructions.
- **Administrative scope** — Applicable permit/inspection, insurance-claim documentation if relevant, cleanup and magnetic nail sweep, schedule and weather.
### TakeShape agent instructions

- **Extract from scan** — Link interior room locations and visible ceiling concerns where relevant. Do not infer roof area, pitch, number of layers or deck condition from the indoor model or home floor area. Import a current roof report/verified plan as a separate source when available.
- **Review key frames** — Use supplemental exterior imagery to identify roof planes, visible covering, penetrations, flashing, valleys, skylights, solar equipment and access. Record apparent defects without diagnosing leak origin. Check imagery against current additions and the correct structure.
- **Ask the homeowner** — Repair or replacement, affected structures, roof history/layers if known, leaks and timing, prior inspection/claim records, desired material/color, ventilation concerns, solar removal responsibility, driveway/dumpster access and schedule.
- **Collect missing evidence** — Obtain plane area/pitch and ridge, hip, valley, eave and rake lengths from a suitable report or qualified measurement source. Request safe exterior and existing attic/inspection images. Never ask a homeowner to access a roof to complete intake.
- **Send to provider** — Measurement report with source and coverage; penetration/accessory inventory; tear-off assumptions; desired assembly; leak history and annotated frames; ventilation scope; delivery, disposal and access details. Provider confirms waste, assembly, code/permit scope and decking repair rates.
- **Quote rule and concealed conditions** — A complete replacement scope can go for virtual pricing with explicit decking/hidden-damage terms. An unknown repair cause or unsupported low-slope assembly needs provider clarification/diagnostic scope. Concealed decking, insulation and structural deterioration remain separate from visible base work.
*Cross-check: EagleView remote roof and wall measurements · IKO roof inspection checklist*

*Research: GAF roof cost factors · GAF roof components*

## Gutter Installation

### Required provider fields

- **System scope** — New installation, replacement, partial run, guards, or downspouts only; exact rooflines included.
- **Takeoff** — Linear feet of each run, corners/end caps, stories and mounting height, and downspout count/length with elbows.
- **Water handling** — Roof drainage areas, desired downspout locations, splash blocks, extensions or underground tie-ins, and discharge destination.
- **Specifications** — Gutter profile/size, seamless or sectional, material, color, hanger method, screens/guards, and matching existing system.
- **Existing conditions** — Fascia/soffit and roof-edge condition, slope, existing drainage failures, and any wood repair or removal needed.
- **Access** — Roof pitch, ladders or lift, trees/landscaping, power lines, gates, and work space for seamless-forming truck.
- **Scope boundaries** — Removal and disposal, fascia repair allowance, buried drainage work, permits if applicable, and leak test/cleanup.
### TakeShape agent instructions

- **Extract from scan** — Use supplemental exterior measurements for a run-by-run schedule; indoor walls or footprint do not establish gutter runs, overhangs or working height. Record each run ID, length, mounting height, corners, outlets and downspout route.
- **Review key frames** — Tag fascia/soffit, drip-edge relationship, visible rot, obstructions, discharge points and mounting access. Link each exterior frame to its run; record unshown sections.
- **Ask the homeowner** — New or replacement runs, material/profile/color preference, guards, downspout locations, overflow history, extensions versus underground connection, removal/disposal, access and known fascia repairs.
- **Collect missing evidence** — Obtain downspout lengths and elbow offsets, guard lengths, relevant roof drainage area/pitch and valley discharge. Provider determines capacity and spacing using local design rainfall; do not use one universal length-only sizing rule.
- **Send to provider** — Dimensioned roofline sketch, gutter/downspout/guard quantities, profile choices, mounting/discharge photos and fascia condition observations; identify drainage, fascia and removal inclusions.
- **Quote rule and concealed conditions** — Send when run geometry, materials, drainage route and access are usable. Quote with stated fascia/hidden-rot terms where needed; uncertain underground drainage scope or unknown run lengths need follow-up. Provider confirms final sizing and installation details.
*Cross-check: Berger gutter and downspout sizing*

*Research: Angi gutter measurement and cost factors*

## Siding Installation

### Required provider fields

- **Scope** — Entire house or specified elevations; siding only versus trim, soffit, fascia, housewrap and exterior paint.
- **Takeoff** — Gross wall area by elevation and height, openings, gables, corners, trim linear feet, and waste from cuts.
- **Existing cladding** — Material, condition, layers, removal and disposal, signs of moisture or rot, and known sheathing damage.
- **Assembly** — New siding material/profile, exposure, color/finish, weather barrier, flashing, insulation, fasteners and manufacturer details.
- **Detail work** — Windows/doors, penetrations, shutters, lights, outlets, vents, transitions, deck attachments, and trim profiles.
- **Access and protection** — Stories, slope, landscaping, overhead obstructions, scaffolding, parking and material staging.
- **Contingencies** — Sheathing/structural repair allowance, permit where needed, lead-paint considerations on older buildings, cleanup and warranty.
### TakeShape agent instructions

- **Extract from scan** — Require exterior measurements or an exterior report for each selected elevation, gable, opening and corner. Indoor RoomPlan geometry alone is not a siding takeoff. Record gross area, openings, trim, starter, soffit and fascia separately.
- **Review key frames** — Identify apparent cladding/profile, visible damage, roof-wall junctions, penetrations, shutters, lights, vents, window/door surrounds and staging constraints. Do not label hidden sheathing or weather barrier as sound.
- **Ask the homeowner** — Full or partial replacement, selected material/profile/color, factory finish or painting, removal layers if known, insulation and trim scope, known leaks/repairs, equipment detach/reset, access, schedule and approvals. If an area seems to have damage, ask for a picture of that specific area.
- **Collect missing evidence** — Obtain all elevation views and scale-supported detail measurements. Let the provider select waste/deduction rules; vinyl guidance often retains ordinary openings as part of waste allowance. Do not automatically subtract every opening or apply a universal waste percentage.
- **Send to provider** — Elevation takeoff, gross/deduction/waste method, accessory quantities, product choices, removal and finish scope, condition frames, equipment/access plan and sheathing repair allowance/unit rates.
- **Quote rule and concealed conditions** — Visible replacement work can be priced virtually from usable exterior data. Carry concealed sheathing, framing and water-barrier issues under explicit change terms. Missing elevations or unresolved product/trim scope require questions before a comparable quote.
*Cross-check: Vinyl Siding Institute measurement and installation manual*

*Research: James Hardie re-side process · James Hardie cost factors*

## Flooring Installation

### Required provider fields

- **Scope and measurement** — Rooms and floor area by room, closets, hallways and stairs; layout direction, pattern, cuts and waste.
- **Product** — Carpet, hardwood, engineered wood, laminate, LVP, tile or stone; grade, plank/tile size, underlayment and installation method.
- **Existing floor** — Material and layers, removal/disposal, adhesive, trim/baseboards and whether old floor can remain.
- **Subfloor** — Wood or concrete, flatness, movement, damage, moisture, leveling and repairs; confirm product-specific requirements.
- **Transitions and details** — Doorways, thresholds, stair treads/risers, vents, cabinets, islands, fireplaces and base/shoe molding.
- **Room readiness** — Furniture/appliance moving, occupied rooms, material storage, HVAC and acclimation where required.
- **Installation logistics** — Access, parking, schedule, dust control, protection of adjacent areas, disposal and who supplies product.
### TakeShape agent instructions

- **Extract from scan** — Calculate selected floor polygons/areas and perimeter by room, with openings, thresholds and visible obstacles. Confirm whether closets and areas under appliances/cabinets are included; label incomplete or occluded geometry. Do not infer stair-tread dimensions or subfloor flatness from a simplified model.
- **Review key frames** — Tag apparent floor type, visible damage, transitions, base/shoe trim, stairs, furniture and appliances. Material and condition remain observations until supported; the scan does not establish hidden floor layers, adhesive or moisture.
- **Ask the homeowner** — Exact product/SKU or allowance, pattern, install method, demolition scope, trim reuse/replacement, floor-height constraints, radiant heat, known substrate/leaks, furniture/appliance/toilet moving and reconnecting, occupancy, timing and material storage.
- **Collect missing evidence** — Request transition and stair measurements, exposed-subfloor images and existing moisture/flatness reports when available. Product requirements control testing/acclimation. Carpet and sheet goods need roll/seam planning; the provider sets layout and waste.
- **Send to provider** — Room takeoff and inclusions, product/pattern, demolition/trim/transition quantities, moving responsibilities, access and condition frames; list substrate/test status and separate rates or allowances for leveling, mitigation and repair.
- **Quote rule and concealed conditions** — A provider may quote installation remotely while naming required pre-installation tests and hidden-subfloor adjustments. Do not block solely because the subfloor is concealed. Unknown install area/product or known unresolved moisture affecting the method needs clarification.
*Cross-check: Armstrong installation and substrate testing guide*

*Research: Shaw laminate installation planning · Shaw tile and stone preparation*

## Window Installation

### Required provider fields

- **Window schedule** — Count and location of each opening; operating style, unit size, shape and grouping.
- **Measurements** — Width and height at multiple points, rough/opening dimensions as appropriate, depth, level, plumb and square; final field verification.
- **Replacement method** — Insert, full-frame or new construction; frame/sill/trim condition and whether opening size changes.
- **Product choices** — Frame material, color, glazing/performance, grille, screens, hardware, egress or specialty features.
- **Surrounding work** — Interior/exterior trim, siding or masonry disturbance, flashing, sealant, insulation, and water-damage repair.
- **Access** — Story, interior furniture/window treatments, exterior landscaping, ladders or lift, and unit delivery route.
- **Scope and logistics** — Old-unit disposal, permits where applicable, finish painting, lead-safe practices where applicable, schedule and warranty.
### TakeShape agent instructions

- **Extract from scan** — Assign stable opening IDs, room/elevation and approximate visible width/height; reconcile repeated captures. Record the measured surface type. A modeled window surface is not a verified rough opening or an order-ready window size.
- **Review key frames** — Tag operating style, groups, frame/trim appearance, visible damage and access. Request interior/exterior pairs and product labels; do not infer hidden sill condition, glazing performance or code compliance from appearance.
- **Ask the homeowner** — Which units, same size or resized, insert/full-frame preference or provider recommendation, material/color, glass/performance, grilles, screens, hardware, known leaks, trim/paint scope, access and schedule.
- **Collect missing evidence** — When needed, collect provider-specified measurements at multiple points, depth and square/plumb evidence. Clearly label glass, frame, visible opening and rough opening. Manufacturer/provider verification for ordering is a separate milestone from quote intake.
- **Send to provider** — Per-opening schedule, scan dimensions and their limitations, paired frames, selected options, replacement approach, trim/repair/disposal scope, access and who will verify final fit.
- **Quote rule and concealed conditions** — Request a virtual quote for defined units/options with explicit measurement and hidden-rot terms. Record whether a measurement visit is required before ordering and any fee; never present it as a requirement-free install booking. Structural resizing needs design/provider review.
*Cross-check: Marvin remote quote measurement guidance*

*Research: Andersen insert replacement criteria · Andersen full-frame replacement criteria*

## Door Installation

### Required provider fields

- **Door schedule** — Number and location; exterior/interior, single/double, slab versus prehung, swing and handing.
- **Measurements** — Panel width/height/thickness, frame and rough opening, jamb depth, squareness, threshold and finished floor height.
- **Configuration** — Sidelights, transom, glass, storm/security door, fire rating or other performance needs.
- **Product and hardware** — Material, finish, insulation, lockset/deadbolt, hinges, closer, smart lock and reuse versus new hardware.
- **Existing conditions** — Frame/sill rot, weather damage, out-of-square opening, adjacent trim/siding and floor transition.
- **Installation work** — Remove/dispose existing unit, repair or resize opening, flashing/weatherstripping, casing/paint/stain and security adjustments.
- **Access and schedule** — Delivery path, occupied/security needs, temporary closure, permits if relevant and final operational check.
### TakeShape agent instructions

- **Extract from scan** — Assign door IDs and room locations, visible opening dimensions and nearby clearance. Deduplicate the same door seen from two rooms, while retaining both faces. Treat panel, unit, trim and rough-opening dimensions as different fields.
- **Review key frames** — Inspect both faces, threshold, frame, hardware and operation; tag observed swing, wear, gaps or rubbing as observations. Do not infer rough-opening size, fire rating or concealed sill condition.
- **Ask the homeowner** — Interior/exterior, slab/pre-hung, single/double, handing and swing with viewing side, product/material/finish, sidelights/transom, lock/hinge/smart-hardware choice, reuse, opening changes and finish/painting scope.
- **Collect missing evidence** — Request provider-specified panel thickness, jamb depth, threshold/floor relationship, square/level evidence and rough-opening data if available. Slab-only work needs hinge positions and bore/backset compatibility. Show the door moving and key dimensions with scale.
- **Send to provider** — Door-by-door schedule, product/hardware choices, dimension types and evidence, operating video, trim/threshold/weatherproofing scope, disposal, access and final-measurement responsibility.
- **Quote rule and concealed conditions** — Quote known replacement scope remotely, with fit verification before custom ordering where required. Hidden frame/sill repairs use agreed change terms. Unresolved handing, hardware fit or structural opening changes need clarification.
*Cross-check: Therma-Tru entry door ordering checklist*

*Research: Pella front door measurement guide*

## Window Cleaning

### Required provider fields

- **Count and type** — Windows by elevation and level; distinguish units from individual panes; note divided lites, sliders, casements, picture windows and glass doors.
- **Scope** — Exterior, interior or both; skylights, storm windows, French panes, mirrors and other specialty glass.
- **Add-ons** — Screens (count/removal/wash/reinstall), tracks, sills, frames, shutters and detailing expectations.
- **Building access** — Number of stories, glass height, roof/ladder/lift access, slope, landscaping, locked areas and water source.
- **Condition** — Routine dirt versus hard-water spots, paint, construction debris, adhesive, oxidation or scratched/coated glass.
- **Interior readiness** — Furniture, blinds/curtains, fragile items, pets and access to each room.
- **Frequency and boundaries** — One-time or recurring, post-construction/restoration as separate scope, exclusions and weather/rescheduling.
### TakeShape agent instructions

- **Extract from scan** — Build a provisional window/glass-door inventory by room and level, then reconcile exterior views. Partial-room scans cannot establish whole-home counts. Deduplicate units and retain provider pricing units separately from detected object count.
- **Review key frames** — Identify visible pane divisions, bay sections, storms, removable grilles, screens, skylights, glass size, interior obstacles and soiling. Distinguish accessible surfaces from insulated glass layers; suspected between-pane haze is not ordinary surface dirt.
- **Ask the homeowner** — Which windows, interior/exterior/both, screens/tracks/sills/frames, recurring or one-time, last cleaning, hard-water or construction deposits, known films/coatings, storms, pets and access. Confirm provider counting convention.
- **Collect missing evidence** — Ask for unscanned elevations and representative close-ups; confirm screens and storm panels, high windows, ground slope and water access. Include safely obtained exterior views; do not ask the homeowner to climb.
- **Send to provider** — Numbered inventory with type, size band, level, sides to clean and add-ons; annotated condition/access frames; exclusions and restoration requests. Obtain homeowner confirmation that all requested units are included.
- **Quote rule and concealed conditions** — Routine work is ready after counts, service depth and access are resolved. Uncertain stain/coating response is a separate test/restoration condition. No supplied scan may be treated as a complete whole-home count without coverage confirmation.
*Cross-check: FISH window counting and estimate guide*

*Research: IWCA non-routine glass cleaning*

## Power Washing

### Required provider fields

- **Surface and method** — Specify concrete, pavers, deck, fence, siding, roof or other surface; pressure washing versus lower-pressure soft washing as appropriate.
- **Measurements** — Cleanable square footage or linear feet by surface, building height, and treatment boundaries.
- **Material sensitivity** — Substrate, coatings, paint age/condition, loose mortar, wood softness, oxidation and potential damage.
- **Soiling** — Dirt, algae/mildew, grease, rust, efflorescence, chewing gum or graffiti; stain removal may be separate.
- **Preparation** — Move vehicles/furniture, cover plants/electrical fixtures, close openings and identify fragile items.
- **Utilities and access** — Water source/flow, power needs, drainage/runoff route, hose length, gates, parking and ladder/lift needs.
- **Treatment and finish** — Detergent, pretreat, hot water if relevant, sealing as an add-on, rinse/cleanup and wastewater handling.
- **Scheduling** — Occupancy, weather, drying time, nearby work and scope exclusions.
### TakeShape agent instructions

- **Extract from scan** — Require capture of each selected exterior surface or supplemental measurements. Record material-associated area/length and height by surface; do not convert interior floor area into house-wash area. Fence sides, rails, stairs and paver joints need separate quantities.
- **Review key frames** — Tag apparent substrate, dirt/algae, rust, oil, efflorescence, graffiti, loose paint, oxidation, damaged joints, plants, electrical fixtures and runoff paths. Do not infer safe pressure or guarantee stain removal from visual classification.
- **Ask the homeowner** — Desired surfaces and outcome, routine wash versus restoration/stripping/resanding/sealing, known coatings, moving vehicles/furniture, water source, access, prior damage and timing.
- **Collect missing evidence** — Request broad views plus stain/coating close-ups, dimension references, spigot/hose route and discharge area. Provider chooses suitable pressure/soft-wash method, chemicals, hot water, protection and containment.
- **Send to provider** — Surface-by-surface quantities/material observations, stain and damage map, requested finish/add-ons, water/access/runoff information and moving/protection responsibilities.
- **Quote rule and concealed conditions** — Quote routine defined washing virtually. For uncertain coatings, oxidation or stubborn stains, ask provider for test-patch/limited-outcome terms. Missing exterior size or method-changing condition requires follow-up; a generic damage caveat cannot define the work.
*Cross-check: Window Genie surface-specific washing guidance · Window Genie condition and stain factors*

*Research: Angi pressure washing pricing factors*

## Gutter Cleaning

### Required provider fields

- **Quantity** — Approximate gutter linear footage, number of runs and downspouts, and stories/working height.
- **Configuration** — Seamless/sectional, gutter guards/screens, roof pitch and unusual rooflines.
- **Debris and condition** — Leaves, pine needles, packed sediment, standing water, visible damage and nearby trees.
- **Service depth** — Hand removal, downspout flush, flow test, gutter exterior brightening and minor repair as separate items.
- **Access** — Ladder placement, slope, landscaping, locked gates, overhead lines and roof-walking limitations.
- **Cleanup** — Bagging/hauling debris, rinsing surrounding surfaces, water availability and recurrence schedule.
- **Exceptions** — Guard removal/reinstallation, blocked underground drains and repair work requiring a separate price.
### TakeShape agent instructions

- **Extract from scan** — Use exterior evidence to map all included gutter runs, lengths, downspouts and actual working heights. Indoor room count or front-story count cannot establish rear walkout heights or detached structures.
- **Review key frames** — Tag visible guards, overflow/stains, debris, damage, trees, ground slope and access obstructions. A ground-level image that cannot see the channel does not establish that it is empty or lightly soiled.
- **Ask the homeowner** — Last cleaning, known clogs, debris type, guard make/removal needs, detached structures, downspout flushing/flow testing, exterior brightening, minor repairs, water source and recurring schedule.
- **Collect missing evidence** — Obtain safe views of every roofline, downspout route and ladder position; use existing photos/reports for obscured interiors. Record unknown buildup honestly and ask the provider to price the appropriate assumption or range.
- **Send to provider** — Run/downspout schedule, heights/guards, condition history and frames, service depth, access, water, debris hauling and cleanup scope.
- **Quote rule and concealed conditions** — Normal cleaning may be quoted remotely with documented debris/guard assumptions. Packed debris, buried-drain blockages and repairs need explicit additional pricing rules; an unknown roofline or access path needs more evidence.
*Cross-check: Mr. Handyman gutter cleaning cost and scope factors*

*Research: Angi gutter cleaning cost factors*

## Maid Services

### Required provider fields

- **Service type** — Standard, deep, move-in/move-out, post-construction or recurring; one-time versus frequency.
- **Home size** — Approximate square footage, levels, bedrooms, full/half baths and other spaces to clean.
- **Room-by-room scope** — Kitchen, bathrooms, living areas, floors, dusting, beds, interior windows and excluded rooms.
- **Current condition** — Last professional clean, buildup, heavy grease/soap scum, clutter and expected tidying level.
- **Floor and surface types** — Carpet, hardwood, tile, stone, delicate finishes and product restrictions.
- **Add-ons** — Inside oven/fridge/cabinets, baseboards, blinds, laundry, dishes, pet hair and organizing.
- **Access** — Parking, entry/keys, alarm, pets, occupants, supplies/equipment provided and water/electricity.
- **Timing** — Target date, preferred hours, recurring interval, estimated cleaning time and rescheduling constraints.
### TakeShape agent instructions

- **Extract from scan** — List selected rooms, usable floor areas, levels and detected large fixtures/obstacles. Confirm bedrooms, full/half baths and fixture counts against frames/homeowner answers. A partial scan is not whole-home square footage.
- **Review key frames** — Tag observable floor/surface types, furniture density, clutter, pet hair, grease and soap buildup; record obscured areas. Do not infer cleanliness or contents inside closed cabinets/appliances.
- **Ask the homeowner** — Standard/deep/move-in/move-out/post-construction, occupied/empty, recurring frequency, last clean, pets, sensitivities, excluded rooms, products/equipment, access and timing. Confirm oven/fridge/cabinet interiors, windows, baseboards/blinds, dishes, laundry, beds and organizing quantities.
- **Collect missing evidence** — Request each included room plus kitchen/bathroom close-ups and interiors only when those services are requested. Ask who clears clutter and whether high-reach areas are included; identify specialized contamination separately.
- **Send to provider** — Room/fixture schedule, cleaned area, task list and rotating-versus-every-visit work, condition frames, add-on quantities, entry/pet/product arrangements and initial versus recurring scope.
- **Quote rule and concealed conditions** — Send when coverage, condition and tasks are clear. Possible hidden issues alone need not stop a routine quote. Heavy clutter, pest/biohazard concerns or unshown requested interiors require provider acceptance or a defined add-on assumption.
*Cross-check: The Cleaning Authority home cleaning quote factors*

*Research: Merry Maids estimate intake · Merry Maids service and pricing factors*

## Handyman Repairs

### Required provider fields

- **Task list** — Each discrete repair with location, desired outcome, priority and whether it is repair or replacement.
- **Symptoms and condition** — What fails, when it began, photos/measurements, water damage, rot or hidden cause suspected.
- **Quantities and dimensions** — Count of fixtures/items, sizes, mounting surfaces and repeat work across rooms.
- **Trades involved** — Carpentry, drywall, paint, plumbing/electrical or other specialist work; clarify licensure/permit needs locally.
- **Materials and parts** — Existing make/model, replacement preference, customer-supplied versus contractor-supplied parts and availability.
- **Access and prep** — Height, ladders, shutoffs, clear work area, fragile finishes, parking and occupied rooms.
- **Quote boundaries** — Diagnostic visit, time/material or fixed scope, minimum charge, concealed-condition change order, cleanup and warranty.
### TakeShape agent instructions

- **Extract from scan** — Locate each task in the room model with an item ID and available dimensions/access clearance. Large detected objects do not establish mounting strength, stud position, hidden wiring or connection compatibility.
- **Review key frames** — Capture the defect, surrounding finish, make/model label and a safe demonstration of the symptom. Distinguish what is observed from a proposed cause and record previous repair history.
- **Ask the homeowner** — Desired repair/replacement outcome, priority, parts supplied, finish match, known cause, shutoffs/access and schedule. For drywall ask area/count, ceiling/wall, texture and paint extent; for trim ask profile/length/finish; for mounting ask item weight/model/kit; for fixtures ask model/connections.
- **Collect missing evidence** — Request close-ups with scale, product specifications and accessible connection/backing evidence. Never ask the homeowner to expose live wiring or perform hazardous diagnostics. Ask the provider whether a defined task or diagnostic appointment should be quoted.
- **Send to provider** — Itemized task schedule, quantities, dimensions, evidence, parts and finish responsibility, access, return-trip/drying needs and cleanup; keep diagnostic work separate from the proposed repair.
- **Quote rule and concealed conditions** — Known tasks can receive virtual prices with explicit hidden-condition terms. If the failure cause determines the work, request diagnostic pricing or further evidence rather than inventing a repair scope. Provider identifies specialist/permit requirements.
*Cross-check: Ace Handyman assessment and repair scope*

*Research: Handyman Connection quote process*

## Interior Remodeling

### Required provider fields

- **Project definition** — Rooms, desired layout and functional goals; drawings, inspiration and exact inclusions/exclusions.
- **Measurements** — Room dimensions, ceiling heights, wall lengths, openings, cabinetry/countertop runs and finish area.
- **Existing conditions** — Age and condition of structure, water damage, load-bearing walls, framing, subfloor and previous alterations.
- **Systems** — Plumbing, electrical, HVAC relocation or capacity; fixtures, outlets, ventilation and lighting plan.
- **Selections** — Cabinets, counters, tile, flooring, fixtures, appliances, paint and allowance levels for unselected items.
- **Demolition and protection** — What is removed, salvage/reuse, dust containment, occupied-home phasing and debris disposal.
- **Approvals and dependencies** — Drawings, permits/inspections, subcontractors, long-lead materials and decisions needed before start.
- **Budget mechanics** — Detailed scope, material/labor breakdown, contingency for concealed conditions, change orders and milestones.
### TakeShape agent instructions

- **Extract from scan** — Build the existing-room plan with wall/opening IDs, floor areas, heights and fixture/cabinet locations. This records the captured existing condition; it does not establish a proposed design, load-bearing status or hidden systems.
- **Review key frames** — Tag visible finishes, damage, fixtures, utility locations and demolition access. Link observable concerns without certifying structural, plumbing, electrical or moisture condition.
- **Ask the homeowner** — Goals, rooms, layout changes, inspiration/plans, budget range, product/finish selections or allowances, salvage, occupied-home phasing, deadline and access. Kitchens need cabinet/appliance/counter/backsplash choices; baths need wet-area, drain/valve, waterproofing, tile and glass scope.
- **Collect missing evidence** — Obtain proposed plans, cabinet/counter/cutout dimensions, utility relocation scope and applicable engineering or trade reports. Custom fabrication dimensions and technical tests are separate milestones; keep design decisions unresolved until selected or explicitly allowed.
- **Send to provider** — Existing and proposed scope, room takeoff, demolition/finish/trade schedule, selections/allowances, evidence, staging/protection/disposal, subcontractor dependencies, permits/inspections and milestone assumptions.
- **Quote rule and concealed conditions** — Cosmetic or fully specified work can go for virtual pricing with named hidden-condition terms. Missing design, structural decisions or uncertain utility scope can prevent a comparable firm quote; request design/diagnostic pricing first. Concealed repairs must be documented and approved through the shared process.
*Cross-check: Houzz Pro remodeling estimate workflow*

*Research: NARI remodeling contractor checklist · NARI existing-conditions evaluation*

## Moving and Junk Removal

### Required provider fields

- **Moving: origin, destination and dates** — Addresses, distance, pickup/delivery windows, local versus long-distance and storage needs.
- **Moving: inventory** — Room-by-room items, boxes, dimensions of large furniture, appliances, fragile/high-value pieces and items not moving.
- **Moving: services** — Packing/unpacking, supplies, disassembly/reassembly, specialty handling and valuation/protection option.
- **Moving: access at both ends** — Floors/stairs/elevators, reservation windows, parking/loading distance, narrow doors, long carries and truck restrictions.
- **Junk: inventory and volume** — Item count/types, estimated truck space, bulk material volume and especially heavy pieces.
- **Junk: material restrictions** — Appliances, electronics, tires, mattresses, paint/chemicals or other regulated items; identify disposal route.
- **Junk: labor and access** — Where items sit, stairs, demolition/disconnection required, loading distance and whether owner can stage items.
- **Both: boundaries** — Donation/recycling versus disposal, landfill fees, sweep-up, timing, added-item policy and price confirmation after inventory is verified.
### TakeShape agent instructions

- **Extract from scan** — Build a provisional large-item inventory by room and capture access dimensions. RoomPlan objects are not a complete household inventory; model boxes do not prove weight, packed volume, stackability or truck load. Keep moving and disposal inventories separate.
- **Review key frames** — Tag visible furniture, appliances, fragile/oversized items, junk types and loading obstacles. Do not infer closed-cabinet contents, material weight or hazardous-item acceptance.
- **Ask the homeowner** — Mark move/keep/discard, boxes and unscanned/offsite items, packing/unpacking, disassembly, specialty handling, valuation/protection, origin/destination, extra stops, storage and dates. Junk also needs dense material type/quantity, special items, disconnection/demolition, donation/recycling and disposal preferences.
- **Collect missing evidence** — Walk all included rooms, closets, storage and safely accessible outbuildings; capture pickup and destination stairs/elevators, parking/carry routes and restrictions. For junk show the full pile from several angles with scale/depth. Obtain precise measurements for tight fits.
- **Send to provider** — Homeowner-confirmed item inventories, size/weight unknowns, boxes and packing level, both-address access, schedule/storage, special handling, disposal acceptance, hauling fees and cleanup. Provider determines crew, vehicle, loading and pricing basis.
- **Quote rule and concealed conditions** — A complete survey can support a provider quote; an approximate object/pile volume alone cannot guarantee it. Require added-item, heavy-load and access-change terms. Ask whether the provider offers a firm remote price or an estimate confirmed on arrival; present that distinction before selection.
*Cross-check: Mayflower virtual survey preparation · Junk King limits of photo estimates*

*Research: United Van Lines quote inputs · 1-800-GOT-JUNK pricing model*

## Landscaping

### Required provider fields

- **Service branch** — Design/installation, maintenance, lawn work, irrigation, drainage or hardscape; define each deliverable.
- **Site measurements** — Turf/bed/hardscape area, bed edges, slopes, access widths and quantity of trees/shrubs.
- **Existing site** — Soil, drainage/wet areas, grading, sun/shade, plant health, weeds, erosion and features to preserve/remove.
- **Design and materials** — Plant species/sizes/count, sod/seed, mulch/stone depth, edging, pavers, lighting and irrigation components.
- **Utilities and water** — Underground/overhead utilities, irrigation zones/condition, water source and any drainage discharge constraints.
- **Prep and labor** — Clearing, excavation, soil amendments, grading, debris hauling, equipment access and protection of existing features.
- **Maintenance plan** — Mowing/bed care frequency, pruning, seasonal service, watering/establishment and warranty expectations.
- **Logistics** — Permits or HOA approval if relevant, delivery/staging, timeline and seasonal planting constraints.
### TakeShape agent instructions

- **Extract from scan** — Indoor RoomPlan contributes no yard takeoff. Use a separately supported exterior capture, site plan or measurement source for turf/bed/hardscape areas, edging lengths, plant locations and gate/access widths. Mark slopes/elevations approximate unless appropriately measured.
- **Review key frames** — Tag apparent grass height, weeds, plant density, obstacles, erosion and visible wet areas. Sun/shade, soil, drainage after rain and underground features need further evidence; one image does not establish them.
- **Ask the homeowner** — Maintenance/design/installation branch and scope. Mowing: frequency, bagging, edging/trimming. Beds: weed removal, mulch material/depth, edging. Plants: species, size/count, remove/preserve. Irrigation: zones/faults/water. Hardscape/drainage: design/materials, excavation and desired outcome.
- **Collect missing evidence** — Obtain exterior coverage, quantities and scale, staging/carry path, rain/soil reports if relevant, known utilities/septic and boundary/approval information. Irrigation diagnostics, precise grading and retaining-wall design require appropriate measurements/expertise.
- **Send to provider** — Service-specific quantities and plans, plant/material schedule, prep/removal/disposal, equipment access, irrigation/water information, establishment care, recurring/seasonal timing and warranty expectations.
- **Quote rule and concealed conditions** — Routine maintenance, defined mulch/planting and cleanup can go for virtual pricing. Unknown soil/roots/rock carry named terms. Missing grade/design data, underground drainage or irrigation faults need provider questions, design or diagnostic scope.
*Cross-check: University of Minnesota landscape site evaluation*

*Research: National Association of Landscape Professionals site analysis*
