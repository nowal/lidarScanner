"""Service quote rubrics: what a provider needs to price each trade.

Quintin's "TakeShape AI Agent -- Service Quote Rubrics" (Oct 6, 2026;
``docs/SERVICE_RUBRICS.md``), condensed to what the runtime can act on. For
each service: the fields a provider needs when applicable, what the scan
can supply, what to ask the homeowner (price-changing first), what evidence
to collect when something is missing, and the rule for when a quote can go
out and how concealed conditions are handled.

Three readers:

- the directives, which tell the agent the rubric for the service in play so
  it prefills from the scan and asks only for what is missing;
- the lead package, which prints the fields and the concealed-condition
  wording so a provider can price the visible scope with named terms;
- the homeowner, who is told once, when quotes come back, what a quote does
  and does not cover.

Nothing here invents a fact: a field the conversation never reached stays
unknown, and that is said rather than filled in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

HOMEOWNER_CONCEALED_WORDING = (
    "Your quote covers the work and conditions described in the TakeShape "
    "scope. The scan cannot show conditions beneath flooring, behind walls or "
    "inside other concealed areas. If the provider discovers an issue that "
    "changes the work, they will document it and explain any change in price "
    "or schedule for your approval before additional work proceeds. Any "
    "allowance or required check is listed in the quote."
)

PROVIDER_CONCEALED_WORDING = (
    "This scope uses the attached scan measurements, key frames and homeowner "
    "answers, with their sources and limitations identified. Concealed "
    "conditions have not been verified. Please state what the base price "
    "includes, any allowances or unit rates, required checks and exclusions. "
    "Document newly discovered issues and obtain homeowner approval of price "
    "or schedule changes before additional work."
)

PROVIDER_RESPONSE_FIELDS = (
    "your business name and confirmation that you serve this area; the quote "
    "request ID; price and its basis; included work and options; taxes or "
    "fees, deposit and payment terms, validity, warranty, estimated duration "
    "and available appointment windows; assumptions and exclusions; and "
    "whether you accept the supplied measurements or need a specific "
    "verification (say which)"
)


@dataclass(frozen=True)
class Rubric:
    key: str
    label: str
    provider_fields: tuple[str, ...]
    scan_supplies: str
    ask: tuple[str, ...]
    evidence: str
    quote_rule: str
    exterior: bool = False   # an indoor RoomPlan scan cannot supply the takeoff


RUBRICS: dict[str, Rubric] = {}


def _add(rubric: Rubric) -> None:
    RUBRICS[rubric.key] = rubric


_add(Rubric(
    key="Painting", label="Painting",
    provider_fields=(
        "Scope and surfaces: interior or exterior; rooms or elevations; walls, ceilings, doors, cabinets, trim; exclusions",
        "Measurements: wall length x height by room or elevation; ceiling area; linear feet and profile of baseboards, crown, casings, railings",
        "Openings and details: doors, windows, built-ins, stairwells, accent walls, high or vaulted ceilings, complex cut-in edges",
        "Existing condition: peeling, holes, cracks, water stains, grease, mildew, wallpaper, texture, bare or previously coated surfaces",
        "Preparation: cleaning, scraping, sanding, patching, caulking, priming, wallpaper removal; whether repairs are included",
        "Finish: colour changes, number of colours and coats, sheen, product grade, stain versus paint, customer-supplied materials",
        "Protection and access: furniture, decor, fixtures, landscaping, flooring, occupied rooms, ladders or lifts, who moves items",
        "Exterior specifics: siding material, height, exposure, shutters, fascia and soffits, substrate repairs",
        "Execution: work hours, drying and curing, cleanup, waste, schedule, touch-up boundaries",
    ),
    scan_supplies=(
        "wall area per room (gross, with door and window openings deducted separately), ceiling area where the "
        "room shape supports it, and door and window opening counts. Both faces of a shared wall stay separate. "
        "Floor area is never wall or ceiling area"
    ),
    ask=(
        "which rooms and surfaces: walls, ceilings, trim, doors (which sides), cabinets, or exterior",
        "colours, sheen or product, and coats, or whether they want a provider recommendation",
        "the preparation and repair standard, and whether repairs are included",
        "who moves and restores belongings, occupancy and timing",
        "touch-up versus a full repaint",
    ),
    evidence=(
        "close-ups with something for scale for repairs and trim; cabinet door and drawer counts; for exterior work, "
        "exterior dimensions and views of every elevation (an indoor scan cannot supply them); known moisture, past "
        "repairs and property age where they affect preparation"
    ),
    quote_rule=(
        "Request a virtual quote once scope, usable dimensions, preparation and finish are resolved or priced as named "
        "options. Possible hidden substrate or moisture issues are disclosed under the change process; a visible active "
        "problem or an uncertain specialty coating needs provider review rather than a caveat."
    ),
))

_add(Rubric(
    key="Roofing", label="Roofing", exterior=True,
    provider_fields=(
        "Job type: repair, partial or full replacement, or new roof; areas included and any detached structures",
        "Roof geometry: area by plane, pitch, stories and height, hips, valleys, dormers, complicated intersections",
        "Existing system: material, age and condition, number of layers, leaks, storm damage, ventilation, visible deck condition",
        "Tear-off and substrate: removal and disposal, underlayment, decking replacement allowance, how concealed damage is priced",
        "Edges and penetrations: ridge and hip, eaves, rakes, chimneys, skylights, vents, pipes, flashings, drip edge, gutters",
        "Proposed assembly: shingle, metal, tile or low-slope system; grade and colour; ice-and-water protection; ventilation; warranty",
        "Access and logistics: steep sections, landscaping, driveway or dumpster placement, delivery, fall protection, power lines",
        "Administrative: permit and inspection, insurance-claim documentation, cleanup and magnetic nail sweep, schedule and weather",
    ),
    scan_supplies=(
        "nothing for the roof itself. Roof area, pitch, layers and deck condition are never inferred from the indoor "
        "model or the floor area; the scan can only locate interior ceiling concerns by room"
    ),
    ask=(
        "repair or replacement, and which structures",
        "roof history and number of layers if known; leaks and when they started",
        "prior inspection or insurance-claim records",
        "desired material and colour; ventilation concerns; who removes solar equipment",
        "driveway or dumpster access and schedule",
    ),
    evidence=(
        "plane areas, pitch, and ridge, hip, valley, eave and rake lengths from a roof report or a qualified "
        "measurement source; safe exterior photos and any existing attic or inspection images. Never ask them onto the roof"
    ),
    quote_rule=(
        "A complete replacement scope can go for virtual pricing with explicit decking and hidden-damage terms. An "
        "unknown repair cause or an unsupported low-slope assembly needs provider clarification or a diagnostic scope. "
        "Concealed decking, insulation and structural deterioration stay separate from the visible base work."
    ),
))

_add(Rubric(
    key="Gutter Installation", label="Gutter installation", exterior=True,
    provider_fields=(
        "System scope: new, replacement, partial run, guards, or downspouts only; exact rooflines included",
        "Takeoff: linear feet per run, corners and end caps, stories and mounting height, downspout count and length with elbows",
        "Water handling: roof drainage areas, downspout locations, splash blocks, extensions or underground tie-ins, discharge destination",
        "Specifications: profile and size, seamless or sectional, material, colour, hanger method, screens or guards, matching the existing system",
        "Existing conditions: fascia, soffit and roof-edge condition, slope, drainage failures, wood repair or removal",
        "Access: roof pitch, ladders or lift, trees and landscaping, power lines, gates, space for a seamless-forming truck",
        "Scope boundaries: removal and disposal, fascia repair allowance, buried drainage work, permits, leak test and cleanup",
    ),
    scan_supplies=(
        "nothing: indoor walls or the footprint do not establish gutter runs, overhangs or working height. Exterior "
        "measurements give a run-by-run schedule"
    ),
    ask=(
        "new or replacement, and which runs",
        "material, profile and colour; guards",
        "downspout locations and overflow history; extensions versus an underground connection",
        "removal and disposal; access; known fascia repairs",
    ),
    evidence=(
        "downspout lengths and elbow offsets, guard lengths, the relevant roof drainage area and pitch. The provider "
        "sizes capacity and spacing for local rainfall; there is no one-length rule"
    ),
    quote_rule=(
        "Send when run geometry, materials, drainage route and access are usable, with stated fascia and hidden-rot terms. "
        "Uncertain underground drainage or unknown run lengths need follow-up first."
    ),
))

_add(Rubric(
    key="Siding Installation", label="Siding installation", exterior=True,
    provider_fields=(
        "Scope: entire house or specified elevations; siding only, or trim, soffit, fascia, housewrap and exterior paint",
        "Takeoff: gross wall area by elevation and height, openings, gables, corners, trim linear feet, waste from cuts",
        "Existing cladding: material, condition, layers, removal and disposal, signs of moisture or rot, known sheathing damage",
        "Assembly: new material and profile, exposure, colour and finish, weather barrier, flashing, insulation, fasteners, manufacturer",
        "Detail work: windows and doors, penetrations, shutters, lights, outlets, vents, transitions, deck attachments, trim profiles",
        "Access and protection: stories, slope, landscaping, overhead obstructions, scaffolding, parking, staging",
        "Contingencies: sheathing or structural repair allowance, permits, lead paint on older buildings, cleanup, warranty",
    ),
    scan_supplies=(
        "the upright surface an exterior walk covered (from the mesh), as a floor on the gross wall area; indoor RoomPlan "
        "geometry alone is not a siding takeoff. Gross area, openings, trim, starter, soffit and fascia are separate quantities"
    ),
    ask=(
        "full or partial replacement, and which elevations",
        "material, profile and colour; factory finish or painting",
        "removal layers if known; insulation and trim scope; known leaks or repairs",
        "equipment to detach and reset; access; schedule and approvals",
        "a photo of any area that looks damaged",
    ),
    evidence=(
        "all elevation views and scale-supported detail measurements. The provider sets waste and deduction rules "
        "(vinyl guidance often keeps ordinary openings as waste); never subtract every opening or apply a universal waste percentage"
    ),
    quote_rule=(
        "Visible replacement work can be priced virtually from usable exterior data, with concealed sheathing, framing "
        "and water-barrier issues under explicit change terms. Missing elevations or unresolved product and trim scope need questions first."
    ),
))

_add(Rubric(
    key="Flooring", label="Flooring",
    provider_fields=(
        "Scope and measurement: rooms and floor area by room, closets, hallways, stairs; layout direction, pattern, cuts, waste",
        "Product: carpet, hardwood, engineered, laminate, LVP, tile or stone; grade, plank or tile size, underlayment, installation method",
        "Existing floor: material and layers, removal and disposal, adhesive, trim and baseboards, whether the old floor can stay",
        "Subfloor: wood or concrete, flatness, movement, damage, moisture, leveling and repairs; product-specific requirements",
        "Transitions and details: doorways, thresholds, stair treads and risers, vents, cabinets, islands, fireplaces, base and shoe",
        "Room readiness: furniture and appliance moving, occupied rooms, storage, HVAC and acclimation",
        "Logistics: access, parking, schedule, dust control, protection of adjacent areas, disposal, who supplies product",
    ),
    scan_supplies=(
        "floor area and perimeter per room from the floor polygon, with openings and thresholds. Whether closets and the "
        "areas under appliances and cabinets are included must be confirmed; stair-tread sizes and subfloor flatness are not inferred"
    ),
    ask=(
        "the exact product or SKU, or an allowance; pattern and installation method",
        "demolition scope; trim reuse or replacement; floor-height constraints; radiant heat",
        "known substrate problems or leaks",
        "who moves furniture, appliances and toilets and reconnects them; occupancy; timing; where material can be stored",
    ),
    evidence=(
        "transition and stair measurements, exposed-subfloor photos, and any moisture or flatness report. Product "
        "requirements decide testing and acclimation; carpet and sheet goods need the provider's seam planning"
    ),
    quote_rule=(
        "A provider may quote installation remotely while naming required pre-installation tests and hidden-subfloor "
        "adjustments; a concealed subfloor alone is no reason to hold back. Unknown area or product, or a known moisture "
        "problem that changes the method, needs clarification."
    ),
))

_add(Rubric(
    key="Window Installation", label="Window installation",
    provider_fields=(
        "Window schedule: count and location of each opening; operating style, unit size, shape, grouping",
        "Measurements: width and height at several points, rough opening, depth, level, plumb and square; final field verification",
        "Replacement method: insert, full-frame or new construction; frame, sill and trim condition; whether the opening size changes",
        "Product: frame material, colour, glazing and performance, grilles, screens, hardware, egress or specialty features",
        "Surrounding work: interior and exterior trim, siding or masonry disturbance, flashing, sealant, insulation, water-damage repair",
        "Access: story, window treatments and furniture, landscaping, ladders or lift, delivery route",
        "Scope and logistics: disposal, permits, finish painting, lead-safe practices, schedule, warranty",
    ),
    scan_supplies=(
        "the opening count and each opening's visible width x height from the RoomPlan surfaces. A modelled window "
        "surface is not a verified rough opening or an order-ready size"
    ),
    ask=(
        "which units; same size or resized",
        "insert or full-frame, or a provider recommendation",
        "material and colour; glass and performance; grilles; screens; hardware",
        "known leaks; trim and paint scope; access; schedule",
    ),
    evidence=(
        "provider-specified measurements at several points, depth, and square and plumb evidence when needed, with "
        "glass, frame, visible opening and rough opening labelled clearly; interior and exterior photo pairs and product labels"
    ),
    quote_rule=(
        "Request a virtual quote for defined units and options with explicit measurement and hidden-rot terms. Record "
        "whether a measurement visit is required before ordering and any fee; never present it as a requirement-free "
        "booking. Structural resizing needs design or provider review."
    ),
))

_add(Rubric(
    key="Door Installation", label="Door installation",
    provider_fields=(
        "Door schedule: number and location; exterior or interior; single or double; slab or prehung; swing and handing",
        "Measurements: panel width, height and thickness; frame and rough opening; jamb depth; squareness; threshold and floor height",
        "Configuration: sidelights, transom, glass, storm or security door, fire rating",
        "Product and hardware: material, finish, insulation, lockset and deadbolt, hinges, closer, smart lock, reuse versus new",
        "Existing conditions: frame or sill rot, weather damage, out-of-square opening, adjacent trim and siding, floor transition",
        "Installation work: remove and dispose, repair or resize the opening, flashing and weatherstripping, casing, paint or stain",
        "Access and schedule: delivery path, occupied or security needs, temporary closure, permits, final operational check",
    ),
    scan_supplies=(
        "the door count and locations with visible opening dimensions and nearby clearance; the same door seen from two "
        "rooms counts once. Panel, unit, trim and rough-opening dimensions are different fields"
    ),
    ask=(
        "interior or exterior; slab or prehung; single or double",
        "handing and swing, and from which side they are looking",
        "product, material and finish; sidelights or transom; lock, hinge and smart hardware; reuse",
        "opening changes; finish and painting scope",
    ),
    evidence=(
        "panel thickness, jamb depth, the threshold-to-floor relationship, square and level evidence, and the rough "
        "opening if available; slab-only work needs hinge positions and bore and backset; a short video of the door moving"
    ),
    quote_rule=(
        "Quote known replacement scope remotely, with fit verification before custom ordering where required. Hidden "
        "frame and sill repairs use agreed change terms. Unresolved handing, hardware fit or structural opening changes need clarification."
    ),
))

_add(Rubric(
    key="Window Cleaning", label="Window cleaning",
    provider_fields=(
        "Count and type: windows by elevation and level; units versus individual panes; divided lites, sliders, casements, picture windows, glass doors",
        "Scope: exterior, interior or both; skylights, storm windows, French panes, mirrors, specialty glass",
        "Add-ons: screens (count, removal, wash, reinstall), tracks, sills, frames, shutters",
        "Building access: stories, glass height, roof, ladder or lift access, slope, landscaping, locked areas, water source",
        "Condition: routine dirt versus hard-water spots, paint, construction debris, adhesive, oxidation, scratched or coated glass",
        "Interior readiness: furniture, blinds and curtains, fragile items, pets, access to each room",
        "Frequency and boundaries: one-time or recurring; post-construction or restoration as separate scope; exclusions; weather",
    ),
    scan_supplies=(
        "a provisional window and glass-door inventory by room (RoomPlan counts openings; the photos may count individual "
        "sashes). A partial scan is not a whole-home count, and the provider's pricing units stay separate from detected objects"
    ),
    ask=(
        "which windows; interior, exterior or both",
        "screens, tracks, sills and frames; recurring or one-time; last cleaning",
        "hard-water or construction deposits; known films or coatings; storm windows",
        "pets and access; and confirm the provider's counting convention",
    ),
    evidence=(
        "unscanned elevations and representative close-ups; confirm screens, storm panels, high windows, ground slope and "
        "water access. Never ask them to climb"
    ),
    quote_rule=(
        "Routine work is ready once counts, service depth and access are resolved. An uncertain stain or coating response "
        "is a separate test or restoration condition. No scan is a complete whole-home count without coverage confirmation."
    ),
))

_add(Rubric(
    key="Power Washing", label="Power washing", exterior=True,
    provider_fields=(
        "Surface and method: concrete, pavers, deck, fence, siding, roof or other; pressure washing versus soft washing",
        "Measurements: cleanable square footage or linear feet by surface, building height, treatment boundaries",
        "Material sensitivity: substrate, coatings, paint age and condition, loose mortar, soft wood, oxidation",
        "Soiling: dirt, algae or mildew, grease, rust, efflorescence, gum, graffiti; stain removal may be separate",
        "Preparation: vehicles and furniture moved, plants and electrical fixtures covered, openings closed, fragile items",
        "Utilities and access: water source and flow, power, runoff route, hose length, gates, parking, ladder or lift",
        "Treatment and finish: detergent, pretreatment, hot water, sealing as an add-on, rinse and cleanup, wastewater handling",
        "Scheduling: occupancy, weather, drying time, nearby work, exclusions",
    ),
    scan_supplies=(
        "the upright surface (walls, siding, fences) and the level ground (drive, walks, patio, any lawn) an exterior walk "
        "covered, from the mesh. Interior floor area is never converted into house-wash area; fence sides, rails, stairs "
        "and paver joints need separate quantities"
    ),
    ask=(
        "which surfaces and the outcome they want: a routine wash, or restoration, stripping, resanding or sealing",
        "known coatings; prior damage",
        "whether that level ground is all driveway or includes lawn or walkway",
        "moving vehicles and furniture; the water source; access; timing",
    ),
    evidence=(
        "broad views plus close-ups of stains and coatings, something for scale, the spigot and hose route, and where "
        "runoff goes. The provider chooses pressure, chemicals, hot water, protection and containment"
    ),
    quote_rule=(
        "Quote routine, defined washing virtually. For uncertain coatings, oxidation or stubborn stains, ask the provider "
        "for test-patch or limited-outcome terms. A missing exterior size or a method-changing condition needs follow-up; "
        "a generic damage caveat cannot define the work."
    ),
))

_add(Rubric(
    key="Gutter Cleaning", label="Gutter cleaning", exterior=True,
    provider_fields=(
        "Quantity: approximate gutter linear footage, number of runs and downspouts, stories and working height",
        "Configuration: seamless or sectional, guards or screens, roof pitch, unusual rooflines",
        "Debris and condition: leaves, pine needles, packed sediment, standing water, visible damage, nearby trees",
        "Service depth: hand removal, downspout flush, flow test, exterior brightening, minor repair as separate items",
        "Access: ladder placement, slope, landscaping, locked gates, overhead lines, roof-walking limits",
        "Cleanup: bagging and hauling debris, rinsing surrounding surfaces, water availability, recurrence schedule",
        "Exceptions: guard removal and reinstallation, blocked underground drains, repairs priced separately",
    ),
    scan_supplies=(
        "nothing from an indoor scan: room count or the front-story count cannot establish rear walkout heights or "
        "detached structures. Exterior evidence maps the runs, lengths, downspouts and working heights"
    ),
    ask=(
        "last cleaning; known clogs; debris type",
        "guard make and whether guards must come off; detached structures",
        "downspout flushing or a flow test; exterior brightening; minor repairs",
        "water source; a recurring schedule",
    ),
    evidence=(
        "safe views of every roofline, downspout route and ladder position; existing photos for channel interiors a "
        "ground-level photo cannot show. Record unknown buildup honestly and have the provider price an assumption or a range"
    ),
    quote_rule=(
        "Normal cleaning may be quoted remotely with documented debris and guard assumptions. Packed debris, buried-drain "
        "blockages and repairs need explicit additional pricing rules; an unknown roofline or access path needs more evidence."
    ),
))

_add(Rubric(
    key="Interior Cleaning", label="Home cleaning",
    provider_fields=(
        "Service type: standard, deep, move-in or move-out, post-construction, or recurring; one-time versus frequency",
        "Home size: approximate square footage, levels, bedrooms, full and half baths, other spaces",
        "Room-by-room scope: kitchen, bathrooms, living areas, floors, dusting, beds, interior windows, excluded rooms",
        "Current condition: last professional clean, buildup, heavy grease or soap scum, clutter, tidying expected",
        "Floor and surface types: carpet, hardwood, tile, stone, delicate finishes, product restrictions",
        "Add-ons: inside oven, fridge or cabinets, baseboards, blinds, laundry, dishes, pet hair, organizing",
        "Access: parking, entry and keys, alarm, pets, occupants, supplies and equipment, water and electricity",
        "Timing: target date, preferred hours, interval, estimated time, rescheduling constraints",
    ),
    scan_supplies=(
        "the rooms scanned with usable floor areas, levels and large fixtures. Bedrooms, baths and fixture counts need "
        "confirming; a partial scan is not whole-home square footage"
    ),
    ask=(
        "standard, deep, move-in or move-out, or post-construction; occupied or empty; recurring and how often",
        "last clean; pets; sensitivities; products or equipment to use or avoid",
        "excluded rooms; oven, fridge and cabinet interiors; windows; baseboards and blinds; dishes; laundry; beds; organizing",
        "entry, alarm and parking; timing",
    ),
    evidence=(
        "each included room, plus kitchen and bathroom close-ups and appliance interiors only when those services are "
        "requested; who clears clutter; whether high-reach areas are included; anything that needs specialized handling, separately"
    ),
    quote_rule=(
        "Send when coverage, condition and tasks are clear; possible hidden issues alone need not hold a routine quote. "
        "Heavy clutter, pest or biohazard concerns, or requested interiors that were not shown need provider acceptance or a defined add-on assumption."
    ),
))

_add(Rubric(
    key="Handyman", label="Handyman repairs",
    provider_fields=(
        "Task list: each discrete repair with location, desired outcome, priority, repair versus replacement",
        "Symptoms and condition: what fails and since when, photos and measurements, water damage, rot, suspected hidden cause",
        "Quantities and dimensions: count of fixtures or items, sizes, mounting surfaces, repeat work across rooms",
        "Trades involved: carpentry, drywall, paint, plumbing or electrical; licensure or permit needs",
        "Materials and parts: existing make and model, replacement preference, who supplies parts, availability",
        "Access and prep: height, ladders, shutoffs, clear work area, fragile finishes, parking, occupied rooms",
        "Quote boundaries: diagnostic visit, time-and-material or fixed scope, minimum charge, change order, cleanup, warranty",
    ),
    scan_supplies=(
        "each task located in the room model with available dimensions and clearance. Detected objects do not establish "
        "mounting strength, stud position, hidden wiring or connection compatibility"
    ),
    ask=(
        "the outcome they want for each task: repair or replacement, and priority",
        "parts they will supply; finish to match; any known cause; shutoffs and access; schedule",
        "drywall: area or count, ceiling or wall, texture, paint extent. Trim: profile, length, finish. Mounting: item weight, model, kit. Fixtures: model and connections",
    ),
    evidence=(
        "close-ups with scale, product specifications, and accessible connection or backing evidence. Never ask them to "
        "expose live wiring or do hazardous diagnostics; ask the provider whether a defined task or a diagnostic appointment should be quoted"
    ),
    quote_rule=(
        "Known tasks get virtual prices with explicit hidden-condition terms. If the failure cause determines the work, "
        "request diagnostic pricing or more evidence rather than inventing a scope; the provider identifies specialist or permit needs."
    ),
))

_add(Rubric(
    key="Interior Remodeling", label="Interior remodeling",
    provider_fields=(
        "Project definition: rooms, desired layout and goals; drawings, inspiration, exact inclusions and exclusions",
        "Measurements: room dimensions, ceiling heights, wall lengths, openings, cabinetry and countertop runs, finish area",
        "Existing conditions: age and condition, water damage, load-bearing walls, framing, subfloor, previous alterations",
        "Systems: plumbing, electrical, HVAC relocation or capacity; fixtures, outlets, ventilation, lighting plan",
        "Selections: cabinets, counters, tile, flooring, fixtures, appliances, paint; allowances for unselected items",
        "Demolition and protection: what is removed, salvage, dust containment, occupied-home phasing, debris disposal",
        "Approvals and dependencies: drawings, permits and inspections, subcontractors, long-lead materials, decisions before start",
        "Budget mechanics: detailed scope, material and labour breakdown, contingency for concealed conditions, change orders, milestones",
    ),
    scan_supplies=(
        "the existing-room plan: wall and opening IDs, floor areas, heights, fixture and cabinet locations. It is the captured "
        "existing condition, not a proposed design, a load-bearing finding or a map of hidden systems"
    ),
    ask=(
        "goals, rooms and layout changes; inspiration or plans; budget range",
        "selections or allowances; salvage; occupied-home phasing; deadline; access",
        "kitchens: cabinets, appliances, counters, backsplash. Baths: wet area, drain and valve, waterproofing, tile, glass",
    ),
    evidence=(
        "proposed plans, cabinet, counter and cutout dimensions, utility relocation scope, and any engineering or trade "
        "reports. Custom fabrication dimensions and technical tests are separate milestones; design decisions stay open until selected or explicitly allowed"
    ),
    quote_rule=(
        "Cosmetic or fully specified work can go for virtual pricing with named hidden-condition terms. Missing design, "
        "structural decisions or uncertain utility scope prevent a comparable firm quote; request design or diagnostic pricing first."
    ),
))

_add(Rubric(
    key="Moving", label="Moving",
    provider_fields=(
        "Origin, destination and dates: addresses, distance, pickup and delivery windows, local versus long-distance, storage",
        "Inventory: room-by-room items, boxes, dimensions of large furniture, appliances, fragile or high-value pieces, items not moving",
        "Services: packing and unpacking, supplies, disassembly and reassembly, specialty handling, valuation or protection",
        "Access at both ends: floors, stairs, elevators, reservation windows, parking and loading distance, narrow doors, long carries, truck limits",
        "Boundaries: added-item policy, and price confirmation once the inventory is verified",
    ),
    scan_supplies=(
        "a provisional large-item inventory by room and the access dimensions. RoomPlan objects are not a complete "
        "household inventory, and modelled boxes do not prove weight, packed volume or stackability"
    ),
    ask=(
        "what moves, stays or is discarded; boxes; anything unscanned or offsite",
        "packing and unpacking; disassembly; specialty handling; valuation or protection",
        "origin, destination, extra stops, storage and dates",
    ),
    evidence=(
        "every included room, closet, storage space and safely reachable outbuilding; stairs, elevators, parking and carry "
        "routes at both ends; precise measurements for tight fits"
    ),
    quote_rule=(
        "A complete survey can support a provider quote; an approximate volume alone cannot guarantee it. Require "
        "added-item, heavy-load and access-change terms, and ask whether the provider gives a firm remote price or an "
        "estimate confirmed on arrival; present that distinction before they choose."
    ),
))

_add(Rubric(
    key="Junk Removal", label="Junk removal",
    provider_fields=(
        "Inventory and volume: item count and types, estimated truck space, bulk material volume, especially heavy pieces",
        "Material restrictions: appliances, electronics, tires, mattresses, paint or chemicals; disposal route",
        "Labour and access: where items sit, stairs, demolition or disconnection required, loading distance, whether the owner can stage items",
        "Boundaries: donation or recycling versus disposal, landfill fees, sweep-up, timing, added-item policy",
    ),
    scan_supplies=(
        "a provisional inventory by room and the access dimensions. Modelled boxes do not prove weight or volume; the "
        "disposal inventory stays separate from anything being moved"
    ),
    ask=(
        "what goes: dense material type and quantity, special or regulated items",
        "disconnection or demolition needed; whether they can stage items",
        "donation or recycling preferences; disposal preferences; dates",
    ),
    evidence=(
        "the full pile from several angles with something for scale and depth; the carry route and loading distance"
    ),
    quote_rule=(
        "A complete survey can support a provider quote; an approximate pile volume alone cannot guarantee it. Require "
        "added-item, heavy-load and access-change terms, and ask whether the price is firm remotely or confirmed on arrival; say which before they choose."
    ),
))

_add(Rubric(
    key="Landscaping", label="Landscaping", exterior=True,
    provider_fields=(
        "Service branch: design and installation, maintenance, lawn work, irrigation, drainage or hardscape; each deliverable",
        "Site measurements: turf, bed and hardscape area, bed edges, slopes, access widths, trees and shrubs",
        "Existing site: soil, drainage and wet areas, grading, sun and shade, plant health, weeds, erosion, features to keep or remove",
        "Design and materials: plant species, sizes and counts, sod or seed, mulch or stone depth, edging, pavers, lighting, irrigation",
        "Utilities and water: underground and overhead utilities, irrigation zones and condition, water source, discharge constraints",
        "Prep and labour: clearing, excavation, soil amendments, grading, hauling, equipment access, protection of existing features",
        "Maintenance plan: mowing and bed care frequency, pruning, seasonal service, establishment watering, warranty",
        "Logistics: permits or HOA approval, delivery and staging, timeline, seasonal planting constraints",
    ),
    scan_supplies=(
        "nothing from an indoor scan. The level ground an exterior walk covered, from the mesh, is a floor on the lawn and "
        "hardscape area; slopes and elevations are approximate unless measured"
    ),
    ask=(
        "maintenance, design or installation, and the scope of each",
        "mowing: frequency, bagging, edging. Beds: weeds, mulch material and depth, edging. Plants: species, size, count, keep or remove",
        "irrigation: zones, faults, water. Hardscape or drainage: design, materials, excavation, the outcome they want",
    ),
    evidence=(
        "exterior coverage with quantities and scale, the staging and carry path, rain or soil reports where relevant, "
        "known utilities and septic, and boundary or HOA information. Irrigation diagnostics, precise grading and retaining-wall design need measurements and expertise"
    ),
    quote_rule=(
        "Routine maintenance, defined mulch and planting, and cleanup can go for virtual pricing. Unknown soil, roots or rock "
        "carry named terms. Missing grade or design data, underground drainage or irrigation faults need provider questions, design or a diagnostic scope."
    ),
))


# Catalog service -> rubric. "Roofing & Siding" and "Window & Door Install"
# cover two or three trades each; the scope options or the message say which.
_VARIANTS: dict[str, tuple[tuple[str, str], ...]] = {
    "Roofing & Siding": (
        (r"gutter", "Gutter Installation"),
        (r"siding|cladding|fascia|soffit", "Siding Installation"),
        (r"roof|shingle", "Roofing"),
    ),
    "Window & Door Install": (
        (r"\bdoors?\b|slab|prehung|entry|patio door", "Door Installation"),
        (r"window", "Window Installation"),
    ),
}
_DEFAULT_VARIANT = {"Roofing & Siding": "Roofing", "Window & Door Install": "Window Installation"}


def in_catalog(service_type: str | None) -> bool:
    """A trade the catalog, the price tables and the partner lists know."""
    from ..home_guide_tools import KNOWN_SERVICE_TYPES

    return bool(service_type) and service_type.strip() in KNOWN_SERVICE_TYPES


def rubric_for(service_type: str | None, scope_options: list[str] | None = None, text: str = "") -> Rubric | None:
    """The rubric for the service in play, or None for a service without one."""
    if not service_type:
        return None
    service = service_type.strip()
    if service in _VARIANTS:
        hints = " ".join([*(scope_options or []), text or ""]).lower()
        for pattern, key in _VARIANTS[service]:
            if re.search(pattern, hints):
                return RUBRICS[key]
        return RUBRICS[_DEFAULT_VARIANT[service]]
    return RUBRICS.get(service)


def directive(
    rubric: Rubric, *, room_measured: bool, surfaces_measured: bool, exterior_capture: bool
) -> str:
    """The rubric as the agent reads it for this conversation."""
    if rubric.exterior and not (surfaces_measured or exterior_capture):
        scan = (
            "nothing usable for this trade: it needs exterior surfaces and this scan is "
            "an interior one (or has not been measured). Say so once, and take the "
            "homeowner's own numbers or ask for an exterior scan; never convert indoor "
            "floor area into this trade's quantity"
        )
    elif rubric.exterior and not surfaces_measured:
        scan = "the exterior capture's photos; the mesh has not reported its surface areas yet, so " + rubric.scan_supplies
    else:
        scan = rubric.scan_supplies
        if not room_measured and not rubric.exterior:
            scan += ". No room measurements are attached to this conversation yet; what you have is what is listed above"
    fields = "; ".join(f.split(":")[0] for f in rubric.provider_fields)
    return (
        f"- SERVICE RUBRIC ({rubric.label}). What a provider needs to price this, when "
        f"applicable: {fields}. From the scan: {scan}. From the homeowner, price-changing "
        f"first and only what they have not already said: {'; '.join(rubric.ask)}. "
        "Prefill from the scan and the conversation, ask for ONE missing thing at a time "
        "when no other question is pending, and never ask them to remeasure a dimension "
        f"the scan supplies. Evidence to collect when it changes the price: {rubric.evidence}. "
        "Never ask them to climb, expose wiring or do anything hazardous to finish this. "
        f"Quote rule: {rubric.quote_rule} Record their answers as scope options and "
        "materials; a fact they have not given is unknown, not a guess."
    )


def package(
    service_type: str | None,
    scope_options: list[str] | None,
    materials: list[str] | None,
    measurements: dict[str, Any] | None,
) -> dict[str, Any]:
    """The rubric section of a lead package: fields, what the scan supplied,
    what the homeowner said, and the concealed-condition terms. Fields the
    conversation never reached are listed as unknown, by name."""
    rubric = rubric_for(service_type, scope_options)
    if rubric is None:
        return {}
    measured = measurements or {}
    from_scan: list[str] = []
    if measured.get("floorAreaSquareFeet"):
        from_scan.append(f"floor area ~{float(measured['floorAreaSquareFeet']):,.0f} sq ft")
    if measured.get("paintableWallSquareFeet"):
        from_scan.append(f"paintable wall ~{float(measured['paintableWallSquareFeet']):,.0f} sq ft")
    if measured.get("perimeterFeet"):
        from_scan.append(f"perimeter ~{float(measured['perimeterFeet']):,.0f} ft")
    if measured.get("windowCount"):
        from_scan.append(f"{measured['windowCount']} window opening(s)")
    if measured.get("windowOpenings"):
        from_scan.append(
            "opening sizes " + ", ".join(
                f"{o['widthFeet']} x {o['heightFeet']} ft" for o in measured["windowOpenings"][:8]
            )
        )
    if measured.get("doorCount"):
        from_scan.append(f"{measured['doorCount']} door(s)")
    scanned = measured.get("scannedSurfacesSquareFeet") or {}
    if scanned.get("upright"):
        from_scan.append(f"upright surface ~{float(scanned['upright']):,.0f} sq ft (walls, siding, fences the walk passed)")
    if scanned.get("ground"):
        from_scan.append(f"level ground ~{float(scanned['ground']):,.0f} sq ft (drive, walks, patio, any lawn scanned)")
    if measured.get("capture"):
        from_scan.append(str(measured["capture"]))
    told = [*(scope_options or []), *(materials or [])]
    return {
        "service": rubric.key,
        "label": rubric.label,
        "providerFields": list(rubric.provider_fields),
        "fromScan": from_scan,
        "scanNote": rubric.scan_supplies,
        "fromHomeowner": told,
        "quoteRule": rubric.quote_rule,
        "concealedConditions": PROVIDER_CONCEALED_WORDING,
        "responseFields": PROVIDER_RESPONSE_FIELDS,
    }


def package_lines(section: dict[str, Any]) -> list[str]:
    """The rubric section as plain text for the ops email."""
    if not section:
        return []
    lines = [f"PROVIDER FIELDS ({section['label']})"]
    lines.append("  What a quote needs, when applicable:")
    for item in section["providerFields"]:
        lines.append(f"  - {item}")
    if section["fromScan"]:
        lines.append("  From the scan: " + "; ".join(section["fromScan"]) + ".")
    else:
        lines.append("  From the scan: nothing measured for this trade. " + section["scanNote"] + ".")
    if section["fromHomeowner"]:
        lines.append("  From the homeowner: " + "; ".join(section["fromHomeowner"]) + " (and the synopsis above).")
    else:
        lines.append("  From the homeowner: see the synopsis above.")
    lines.append("  Anything not covered above is UNKNOWN: ask through the app rather than assume.")
    lines.append(f"  Quote rule: {section['quoteRule']}")
    lines.append(f"  Concealed conditions: {section['concealedConditions']}")
    lines.append(f"  In your quote, please state: {section['responseFields']}.")
    return lines
