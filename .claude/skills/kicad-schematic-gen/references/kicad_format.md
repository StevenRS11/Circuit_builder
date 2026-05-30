# KiCad .kicad_sch File Format Reference

## File Structure

A `.kicad_sch` file is an S-expression tree with this top-level structure:

```
(kicad_sch
  (version 20230121)
  (generator "your_generator_name")
  (uuid "root-uuid")
  (paper "A4")
  
  (title_block
    (title "Board Name")
    (date "2026-03-14")
    (rev "1.0")
  )
  
  (lib_symbols ...)        ;; Symbol library definitions used in this schematic
  
  ;; Placed elements:
  (junction ...)            ;; Wire junctions
  (no_connect ...)          ;; Unused pin markers
  (wire ...)                ;; Wires
  (label ...)               ;; Local net labels  
  (global_label ...)        ;; Cross-sheet labels
  (symbol ...)              ;; Placed component instances
  
  (sheet_instances
    (path "/" (page "1"))
  )
)
```

## Key Sections

### lib_symbols
Contains the FULL symbol definition for every unique component type used. This is a self-contained copy — the schematic doesn't reference external .kicad_sym files at runtime.

Each symbol needs:
- Property definitions (Reference, Value, Footprint, Datasheet)
- Graphics (rectangles, lines, arcs for the body)
- Pin definitions with number, name, type, position

### Symbol (placed instance)
```
(symbol
  (lib_id "Library:SymbolName")
  (at X Y ROTATION)
  (unit 1)
  (in_bom yes)
  (on_board yes)
  (dnp no)
  (uuid "unique-id")
  (property "Reference" "R1" (at X Y ROT) (effects (font (size 1.27 1.27))))
  (property "Value" "10k" (at X Y ROT) (effects (font (size 1.27 1.27))))
  (property "Footprint" "..." (at X Y ROT) (effects (font (size 1.27 1.27)) hide))
  (property "Datasheet" "~" (at X Y ROT) (effects (font (size 1.27 1.27)) hide))
  (pin "1" (uuid "pin-uuid-1"))
  (pin "2" (uuid "pin-uuid-2"))
  (instances
    (project ""
      (path "/" (reference "R1") (unit 1))
    )
  )
)
```

### Wire
```
(wire
  (pts (xy X1 Y1) (xy X2 Y2))
  (stroke (width 0) (type default))
  (uuid "wire-uuid")
)
```

### Label (local net name)
```
(label "NET_NAME"
  (at X Y ROTATION)
  (effects (font (size 1.27 1.27)))
  (uuid "label-uuid")
)
```

### Global Label
```
(global_label "NET_NAME"
  (shape input|output|bidirectional|tri_state|passive)
  (at X Y ROTATION)
  (effects (font (size 1.27 1.27)))
  (uuid "label-uuid")
  (property "Intersheetrefs" "${INTERSHEET_REFS}"
    (at X Y 0)
    (effects (font (size 1.27 1.27)) hide)
  )
)
```

### Power Symbol
Power symbols (VCC, GND, +3V3, etc.) are regular symbols with special properties:
- `in_bom no`
- `on_board no`
- They define a power pin that creates a global net

```
;; In lib_symbols:
(symbol "power:GND"
  (power)
  (pin_names (offset 0))
  (in_bom no)
  (on_board no)
  (property "Reference" "#PWR" (at 0 -2.54 0) (effects (font (size 1.27 1.27)) hide))
  (property "Value" "GND" (at 0 -3.81 0) (effects (font (size 1.27 1.27))))
  (symbol "GND_0_1"
    (polyline
      (pts (xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54) (xy -1.27 -1.27) (xy 0 -1.27))
      (stroke (width 0) (type default))
      (fill (type none))
    )
  )
  (symbol "GND_1_1"
    (pin power_in line (at 0 0 270) (length 0) (name "GND" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
  )
)
```

## Coordinate System

- Origin: top-left of the schematic sheet
- X: increases rightward (normal)
- Y: increases downward (inverted from typical math coordinates)
- Units: millimeters
- Grid: 1.27mm (50 mil) — ALL positions must be grid-aligned
- Common grid values: 0, 1.27, 2.54, 3.81, 5.08, 6.35, 7.62, 8.89, 10.16, 11.43, 12.7 ...
- Rotation: degrees (0, 90, 180, 270)

## Pin Types

| Token | Description |
|-------|-------------|
| input | Input pin |
| output | Output pin |
| bidirectional | Bidirectional I/O |
| tri_state | Tri-state output |
| passive | Passive (R, C, L) |
| free | Not internally connected |
| unspecified | Type not specified |
| power_in | Power input (VCC, GND pins) |
| power_out | Power output (regulator output) |
| open_collector | Open collector/drain |
| open_emitter | Open emitter/source |
| no_connect | Not connected internally |

## Common Library IDs

| Library ID | Description |
|-----------|-------------|
| Device:R | Resistor |
| Device:C | Capacitor |
| Device:C_Polarized | Polarized capacitor |
| Device:L | Inductor |
| Device:LED | LED |
| Device:D | Diode |
| Device:D_Schottky | Schottky diode |
| Device:D_Zener | Zener diode |
| Device:Q_NPN_BEC | NPN transistor |
| Device:Q_PNP_BEC | PNP transistor |
| Device:Q_NMOS_GDS | N-channel MOSFET |
| Device:Q_PMOS_GDS | P-channel MOSFET |
| Connector_Generic:Conn_01x02 | 2-pin connector |
| Connector_Generic:Conn_01x03 | 3-pin connector |
| Connector_Generic:Conn_01x04 | 4-pin connector |
| Connector:USB_C_Receptacle_USB2.0 | USB-C 2.0 connector |
| power:GND | Ground symbol |
| power:VCC | VCC power symbol |
| power:+3V3 | 3.3V power symbol |
| power:+5V | 5V power symbol |

## Common Footprints

| Footprint | Description |
|-----------|-------------|
| Resistor_SMD:R_0402_1005Metric | 0402 resistor |
| Resistor_SMD:R_0603_1608Metric | 0603 resistor |
| Resistor_SMD:R_0805_2012Metric | 0805 resistor |
| Capacitor_SMD:C_0402_1005Metric | 0402 capacitor |
| Capacitor_SMD:C_0603_1608Metric | 0603 capacitor |
| Capacitor_SMD:C_0805_2012Metric | 0805 capacitor |
| LED_SMD:LED_0603_1608Metric | 0603 LED |
| Package_TO_SOT_SMD:SOT-23-5 | SOT-23-5 |
| Package_TO_SOT_SMD:SOT-23 | SOT-23 (3-pin) |
| Package_SO:SOIC-8_3.9x4.9mm_P1.27mm | SOIC-8 |
| Package_DFN_QFN:QFN-16 | QFN-16 (varies by pitch) |
| Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical | 2-pin header |
| Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical | 4-pin header |
| Connector_USB:USB_C_Receptacle_GCT_USB4125 | USB-C receptacle |
