# Proven source parser repairs

Lava and Sunbeam fixtures derive exclusively from the accepted September 14
recon captures. No further live GETs were needed. Source identities and
allowlist entries are unchanged; fetchers and GUI delivery wiring are unchanged.

## Lava

Literal `<th>`/`<td>` matching ignored ordinary width attributes. A1 Vibe,
A3 Vibe, A5 23 and A7 Torch now parse complete current specs, including
battery capacities 1000, 1750, 1000 and 2575 mAh respectively, GSM 900/1800MHz
and USB Connectivity Yes. Matching requires a complete adjacent th/td pair in
a closed table row, preserving the previous text cleaning and first-label
rule. Standard-library visibility filtering excludes comments, hidden/aria-hidden
subtrees and explicitly display:none tables/rows/cells. Collapsed accordion
divs still contain current specs and are retained.

Native field keys and values remain available. Only evidence-supported aliases
join the existing meaningful-field contract:

| Source label/value evidence | Shared key | Limitation |
|---|---|---|
| Type: integer mAh Li-ion | battery-capacity | Requires the explicit capacity/unit/chemistry pattern; other Type values stay native |
| Operating Freq / Operating Frequency: GSM frequency list MHz | network-band-gsm | Requires explicit GSM and MHz; unknown network text stays native |
| USB Connectivity | usb-connection | Keeps Yes/No or source text; does not infer connector type |
| Expandable Memory | external-storage | Keeps the source capacity text; no guessed units |

`Discovery.raw.source_specs` retains all accepted source labels/values before
aliasing. Adding shared aliases can expose newly populated specification fields
on the first future run; it is information gain, never a new handset identity.
The A1 Vibe Expandable Memory section is commented out and remains excluded;
its separate native Memory Card field is retained without an extra guessed alias.
Battery, GSM-band and USB value-change regressions exercise meaningful diffing.

Eleven projected real listing/detail fixtures include all four affected products
and seven previously working products. The seven keep their existing native
fields unchanged. Their original `view_details_specs` strings are preserved;
response URL/time/hash provenance accompanies the fixtures.

## Sunbeam

Scalar minor-unit numeric strings now convert through Decimal using the API's
`currency_minor_unit`. String/integer 35900 with unit 2 means USD 359.00;
unit 0 and unit 3 are also covered. Invalid/null/empty values and invalid units
remain None. An absent unit retains the prior two-decimal convention for legacy
payload compatibility; current captured payloads all provide unit 2. Raw price
objects stay in `Discovery.raw.store_prices`. No captured price range is present:
all 51 `price_range` values are null, so no range aggregation was invented.

Sunbeam Premium is rejected only when its first-party service category,
SKU F1PSS-1 and official `/product/sunbeam-f1-premium-service/` URL agree.
Service-category overlap alone does not reject an unrelated handset. All 29
captured real handset SKU identities remain accepted, with current USD prices.
The catalogue floor remains six.

The code's `MEANINGFUL_FIELDS` has a reserved `price` field, but collectors
store commerce price at top-level Discovery, and the pipeline compares only
meaningful `fields` for complete observations. There is no ratified top-level
price alert policy. This repair makes current price observable and hashable;
it does not add price-change notifications or fabricate historical prices.
Price alerting remains separate work.

## Documentation and runtime limits

Promoted collectors' experimental-only module introductions were stale and
now agree with the existing production allowlist. Lava's transport ticket
records implemented commit d2d8055 rather than claiming the repair is pending;
historical failures and soak requirements remain attributed to pre-fix code.
README cloud revision/scheduling claims are historical or require host
verification. Hetzner was not inspected in this mission.
