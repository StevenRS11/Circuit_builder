# Bill of Materials — battery_3s (Path A', MAX77961)

> Single-chip integrated buck-boost charger with system power-path. Replaces the Path B
> BQ24650 + LM5050-1/LTC4412 ideal-diode MUX. See `battery_3s_02_candidates.md` + `battery_3s_05_review.md`.

| Ref | Value | Part Number | Package | KiCad Symbol (lib_id) | Footprint (KiCad) | In Stock? | Qty | Supplier | Supplier PN | Unit Price | Notes |
|-----|-------|-------------|---------|-----------------------|-------------------|-----------|-----|----------|-------------|------------|-------|
| U1 | CH224K | CH224K | SSOP-10-EP | Interface_USB:CH224K | Package_SO:SSOP-10-1EP_3.9x4.9mm_P1mm_EP2.1x3.3mm | yes | 1 | LCSC | C970725 | $0.29 | PD sink, 15V strap |
| U2 | MAX77961EFV06+ | MAX77961EFV06+ | FC2QFN-30 4x4 | MAX77961EFV06_:MAX77961EFV06_ | MAX77961EFV06_:IC_MAX77961EFV06_ | yes | 1 | DigiKey | MAX77961EFV06+ | $6.50 | Buck-boost 2S/3S charger, integral SYS power-path, autonomous. Vendor SnapEDA symbol+footprint installed; 30-lead FC2QFN, NO exposed pad (confirmed datasheet pg 20, code F304A4F+1). Alt: MAX77960EFV06+ (3A) LCSC C1020018. |
| U3 | AP63200WU-7 | AP63200WU-7 | TSOT-23-6 | Regulator_Switching:AP63203WU | Package_TO_SOT_SMD:TSOT-23-6 | yes | 1 | LCSC | C2071868 | $0.16 | 5V buck (adjustable FB) |
| U6 | USBLC6-2SC6 | USBLC6-2SC6 | SOT-23-6 | Power_Protection:USBLC6-2SC6 | Package_TO_SOT_SMD:SOT-23-6 | yes | 1 | LCSC | C2827654 | $0.02 | USB ESD |
| Q1 | AO4407A | AO4407A | SO-8 | Custom:AO4407A | Package_SO:SOIC-8_3.9x4.9mm_P1.27mm | yes | 1 | LCSC | C16072 | $0.18 | Battery reverse-pol + soft-start (R6) |
| Q6 | AO4407A | AO4407A | SO-8 | Custom:AO4407A | Package_SO:SOIC-8_3.9x4.9mm_P1.27mm | yes | 1 | LCSC | C16072 | $0.18 | Output load switch |
| Q7 | AO3400A | AO3400A | SOT-23 | Transistor_FET:AO3400A | Package_TO_SOT_SMD:SOT-23 | yes | 1 | LCSC | C20917 | $0.08 | Load-switch driver |
| Q8 | AO3400A | AO3400A | SOT-23 | Transistor_FET:AO3400A | Package_TO_SOT_SMD:SOT-23 | yes | 1 | LCSC | C20917 | $0.08 | DISQBAT (ship-mode) driver |
| Q9 | AO3400A | AO3400A | SOT-23 | Transistor_FET:AO3400A | Package_TO_SOT_SMD:SOT-23 | yes | 1 | LCSC | C20917 | $0.08 | Charge-while-off override (USB present -> DISQBAT low) |
| L1 | 3.3uH | SWPA6045S3R3NT (3.3uH 8A) | 6045 | Device:L | Inductor_SMD:L_7.3x7.3_H3.5 | yes | 1 | LCSC | C167219 | $0.20 | Buck-boost inductor (Isat >= 8A) |
| L2 | 4.7uH | CYA0630-4R7M | 7.2x6.6 | Device:L | Inductor_SMD:L_7.3x7.3_H3.5 | yes | 1 | LCSC | C5189748 | $0.17 | 5V buck inductor |
| RS1 | 10m | 10mR 1W 1% | 1206 | Device:R | Resistor_SMD:R_1206_3216Metric | yes | 1 | LCSC | C914291 | $0.03 | Input current sense |
| RT1 | 10k NTC 103AT | NCP15XH103F03RC | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | C13564 | $0.05 | Pack temp sense (THM) |
| D1 | Green | LED Green 0805 | 0805 | Device:LED | LED_SMD:LED_0805_2012Metric | yes | 1 | LCSC | C72043 | $0.02 | Power LED |
| D2 | Red | LED Red 0805 | 0805 | Device:LED | LED_SMD:LED_0805_2012Metric | yes | 1 | LCSC | C2286 | $0.02 | Charge LED (STAT) |
| D3 | SMF15A | SMF15A | SOD-123FL | Device:D_TVS | Diode_SMD:D_SOD-123F | yes | 1 | LCSC | C190158 | $0.02 | VBUS TVS |
| D7 | SS34 | SS34 | SMA | Device:D | Diode_SMD:D_SMA | yes | 1 | LCSC | C8678 | $0.02 | BATT->SYS inrush Schottky (3A) |
| D8 | SMF15A | SMF15A | SOD-123FL | Device:D_TVS | Diode_SMD:D_SOD-123F | yes | 1 | LCSC | C190158 | $0.02 | VBUS TVS (2nd, parallel w/ D3 — flanks J1 VBUS pins) |
| J1 | USB-C | USB4125-GF-A | USB-C | (connector) | Connector_USB:USB_C_Receptacle_GCT_USB4125-xx-x_6P_TopMnt_Horizontal | yes | 1 | LCSC | C2693884 | $0.55 | USB-C PD receptacle |
| J2 | JST-PH 2-pin | S2B-PH-SM4-TB | JST-PH SMD | (connector) | Connector_JST:JST_PH_S2B-PH-SM4-TB_1x02-1MP_P2.00mm_Horizontal | yes | 1 | LCSC | C295747 | $0.12 | Battery main |
| J4 | JST-PH 2-pin | S2B-PH-SM4-TB | JST-PH SMD | (connector) | Connector_JST:JST_PH_S2B-PH-SM4-TB_1x02-1MP_P2.00mm_Horizontal | yes | 1 | LCSC | C295747 | $0.12 | VSTEPPER output |
| J5 | JST-PH 2-pin | S2B-PH-SM4-TB | JST-PH SMD | (connector) | Connector_JST:JST_PH_S2B-PH-SM4-TB_1x02-1MP_P2.00mm_Horizontal | yes | 1 | LCSC | C295747 | $0.12 | +5V output |
| J6 | JST-PH 3-pin | S3B-PH-SM4-TB | JST-PH SMD | (connector) | Connector_JST:JST_PH_S3B-PH-SM4-TB_1x03-1MP_P2.00mm_Horizontal | yes | 1 | LCSC | C265101 | $0.14 | Status: STAT, INOKB, GND |
| J7 | JST-PH 2-pin | S2B-PH-SM4-TB | JST-PH SMD | (connector) | Connector_JST:JST_PH_S2B-PH-SM4-TB_1x02-1MP_P2.00mm_Horizontal | yes | 1 | LCSC | C295747 | $0.12 | Master switch (external) -> SYSTEM_EN |
| R1 | 56k | 56k 5% 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | CH224K CFG1 -> 15V |
| R2 | 10k | 10k 5% 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | CH224K PG pull-up |
| R3 | 8.66k | 8.66k 1% 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | CNFG = 3S |
| R4 | 178k | 178k 1% 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | VSET = 12.15V (4.05V/cell) |
| R5 | 54.9k | 54.9k 1% 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | ISET = 1.5A |
| R6 | 14k | 14k 1% 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | INLIM = 3A input limit |
| R7 | 226k | 226k 1% 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | ITO = 100mA top-off |
| R8 | 10k | 10k 1% 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | THM bias to AVL |
| R9 | 4.7R | 4.7R 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | AVL<->PVL decouple |
| R10 | 10k | 10k 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | STAT pull-up |
| R11 | 200k | 200k 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | INOKB pull-up |
| R12 | 10k | 10k 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | SCL pull-up (autonomous) |
| R13 | 10k | 10k 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | SDA pull-up (autonomous) |
| R14 | 10k | 10k 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | INTB pull-up (autonomous) |
| R15 | 10R | 10R 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | CSINP Kelvin filter |
| R16 | 10R | 10R 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | CSINN Kelvin filter |
| R17 | 52.3k | 52.3k 1% 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | Buck FB top -> 5.0V |
| R18 | 10k | 10k 1% 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | Buck FB bottom |
| R19 | 1k | 1k 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | Green LED |
| R20 | 1k | 1k 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | Red LED |
| R21 | 100k | 100k 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | SYSTEM_EN pull-down |
| R22 | 100k | 100k 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | Q6 gate pull-up to SYS |
| R23 | 1M | 1M 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | DISQBAT default pull-up to BATT (ship when off) |
| R24 | 100k | 100k 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | VIN_DET divider top (from VBUS_USB) |
| R25 | 100k | 100k 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | VIN_DET divider bottom (Q9 gate to GND) |
| R26 | 470k | 470k 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | Q1 gate to GND (slow inrush ramp) |
| R27 | 100k | 100k 0805 | 0805 | Device:R | Resistor_SMD:R_0805_2012Metric | yes | 1 | LCSC | — | — | SYSTEM_EN divider top (gate Vgs limit) |
| C1 | 10uF | 10uF 35V X7R 1210 | 1210 | Device:C | Capacitor_SMD:C_1210_3225Metric | yes | 1 | LCSC | — | — | CHGIN input cap |
| C2 | 10uF | 10uF 35V X7R 1210 | 1210 | Device:C | Capacitor_SMD:C_1210_3225Metric | yes | 1 | LCSC | — | — | CHGIN input cap |
| C3 | 10uF | 10uF 35V X7R 1210 | 1210 | Device:C | Capacitor_SMD:C_1210_3225Metric | yes | 1 | LCSC | — | — | VBUS_USB bulk |
| C4 | 100nF | 100nF 50V X7R 0805 | 0805 | Device:C | Capacitor_SMD:C_0805_2012Metric | yes | 1 | LCSC | — | — | CH224K VDD decouple |
| C5 | 47uF | 47uF 25V X5R 1210 | 1210 | Device:C | Capacitor_SMD:C_1210_3225Metric | yes | 1 | LCSC | — | — | SYS cap |
| C6 | 47uF | 47uF 25V X5R 1210 | 1210 | Device:C | Capacitor_SMD:C_1210_3225Metric | yes | 1 | LCSC | — | — | SYS cap |
| C7 | 47uF | 47uF 25V X5R 1210 | 1210 | Device:C | Capacitor_SMD:C_1210_3225Metric | yes | 1 | LCSC | — | — | SYS cap (3x for 3S) |
| C8 | 10uF | 10uF 25V X7R 0805 | 0805 | Device:C | Capacitor_SMD:C_0805_2012Metric | yes | 1 | LCSC | — | — | SYSA sense cap |
| C9 | 10uF | 10uF 25V X7R 0805 | 0805 | Device:C | Capacitor_SMD:C_0805_2012Metric | yes | 1 | LCSC | — | — | BATT cap |
| C10 | 220nF | 220nF 6.3V 0402 | 0402 | Device:C | Capacitor_SMD:C_0402_1005Metric | yes | 1 | LCSC | — | — | BST1 bootstrap |
| C11 | 220nF | 220nF 6.3V 0402 | 0402 | Device:C | Capacitor_SMD:C_0402_1005Metric | yes | 1 | LCSC | — | — | BST2 bootstrap |
| C12 | 4.7uF | 4.7uF 6.3V X7R 0805 | 0805 | Device:C | Capacitor_SMD:C_0805_2012Metric | yes | 1 | LCSC | — | — | PVL cap |
| C13 | 4.7uF | 4.7uF 6.3V X7R 0805 | 0805 | Device:C | Capacitor_SMD:C_0805_2012Metric | yes | 1 | LCSC | — | — | AVL cap |
| C14 | 100nF | 100nF 0402 | 0402 | Device:C | Capacitor_SMD:C_0402_1005Metric | yes | 1 | LCSC | — | — | CSINP filter |
| C15 | 100nF | 100nF 0402 | 0402 | Device:C | Capacitor_SMD:C_0402_1005Metric | yes | 1 | LCSC | — | — | CSINN filter |
| C16 | 100nF | 100nF 25V 0805 | 0805 | Device:C | Capacitor_SMD:C_0805_2012Metric | yes | 1 | LCSC | — | — | Q1 soft-start (inrush, ~47ms) |
| C17 | 10uF | 10uF 50V X7R 1210 | 1210 | Device:C | Capacitor_SMD:C_1210_3225Metric | yes | 1 | LCSC | — | — | Buck Cin (VSTEPPER, 15V) |
| C18 | 22uF | 22uF 16V X7R 0805 | 0805 | Device:C | Capacitor_SMD:C_0805_2012Metric | yes | 1 | LCSC | — | — | Buck Cout |
| C19 | 22uF | 22uF 16V X7R 0805 | 0805 | Device:C | Capacitor_SMD:C_0805_2012Metric | yes | 1 | LCSC | — | — | Buck Cout |
| C20 | 100nF | 100nF 16V 0805 | 0805 | Device:C | Capacitor_SMD:C_0805_2012Metric | yes | 1 | LCSC | — | — | Buck BST |
| C21 | 10uF | 10uF 16V X7R 0805 | 0805 | Device:C | Capacitor_SMD:C_0805_2012Metric | yes | 1 | LCSC | — | — | +5V bulk |
| C22 | 100nF | 100nF 16V 0805 | 0805 | Device:C | Capacitor_SMD:C_0805_2012Metric | yes | 1 | LCSC | — | — | +5V decouple |
| C23 | 10nF | 10nF 0402 | 0402 | Device:C | Capacitor_SMD:C_0402_1005Metric | yes | 1 | LCSC | — | — | CSINP/CSINN differential filter |

## Summary

- **73 lines.** Estimated parts cost ≈ **$10.5/board** (qty 1), dominated by U2 MAX77961 (~$6.50).
- **Rev 2026-06-01:** +Q9 + R24/R25 (charge-while-off override); −J3 + D4/D5/D6 (dead balance block removed — balancing is in the pack BMS). Net −1 part, cost ≈ unchanged.
- **vs Path B:** one integrated charger replaces BQ24650 + LM5050-1 + LTC4412 + 4 power FETs + sense + charger inductor — the SYS power path is now on-chip (buck-boost, instant-on, autonomous).
- **Cap derating:** CHGIN/VBUS/buck-Cin caps 35–50V (15V nets); SYS 25V (12.6V); 5V-net caps 16V.

## PCBway notes
- **U2 MAX77961** FC2QFN-30 (0.4mm) is assemblable. **Vendor SnapEDA symbol + footprint installed** (`MAX77961EFV06_:MAX77961EFV06_` / `:IC_MAX77961EFV06_`) and bound in the schematic. The part is a **30-lead FC2QFN with NO exposed pad** (confirmed datasheet pg 20 + pg 8, code F304A4F+1; thermal path is junction-to-case via the leads) — the 30-pin symbol+footprint are correct, no center EP pad to add. Confirm MAX77961EFV06+ live stock at DigiKey/Mouser (or use the pin-identical 3A MAX77960EFV06+, LCSC C1020018; check SYS+charge current headroom).
- **Power FETs Q1/Q6** (AO4407A SO-8): custom 8-pin symbol with all source/drain pads mapped (verified by check_cards). (Former Q2 USB reverse-block removed — R7 now via MAX77961 true-load-disconnect.)

**Status:** DRAFT (Path A'). Run check_pcbway + check_requirements; then route.
