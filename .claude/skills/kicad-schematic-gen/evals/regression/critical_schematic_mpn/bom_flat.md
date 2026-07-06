# Flat BOM — critical_schematic_mpn (negative regression)

Frozen reproduction of the "clean schematic, empty PCBWay BOM" class: a fitted part
whose Part Number is blank / a distributor code / a description bakes into a symbol
MPN the PCBWay plugin cannot resolve. The [CRITICAL] schematic-MPN gate must bite.

| Ref | Value | Part Number | Manufacturer | Package | Footprint |
|-----|-------|-------------|--------------|---------|-----------|
| U1 | AP2112K-3.3 | AP2112K-3.3TRG1 | Diodes Inc | SOT-23-5 | Package_TO_SOT_SMD:SOT-23-5 |
| U2 | CH340G |  |  | SOP-16 | Package_SO:SOIC-16_3.9x9.9mm_P1.27mm |
| R1 | 10k | C25804 | LCSC | 0805 | Resistor_SMD:R_0805_2012Metric |
| C1 | 100nF | 100nF 50V X7R 0805 | Samsung | 0805 | Capacitor_SMD:C_0805_2012Metric |
| C2 | 1uF | CL21B105KBFNNNE | Samsung | 0805 | Capacitor_SMD:C_0805_2012Metric |
