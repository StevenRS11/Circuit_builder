# Implementation Reference — {PROJECT_NAME}

> For each IC and non-trivial component, this document captures the datasheet-derived
> information needed to build the schematic correctly. **User reviews pinouts and
> application circuits before schematic generation begins.**

---

## Component: {Reference} — {Part Number} ({Role})

### Pinout

| Pin # | Pin Name | Type        | Connected To        | Notes                    |
|-------|----------|-------------|---------------------|--------------------------|
| 1     | {name}   | {in/out/pwr/NC} | {net or component.pin} | {any special notes}   |
| 2     | {name}   | {type}      | {connection}        |                          |

### Application Circuit (from datasheet)

```
{ASCII art or text description of the recommended application circuit
from the datasheet. Include specific component values called out.}

Example:
    VIN ──┬── [C_in 1uF] ──┐
          │                 │
         VIN              GND
          │    AP2112K     │
         EN               GND
          │                 │
          ├── tied to VIN   │
         VOUT               │
          │                 │
          ├── [C_out 1uF] ──┘
          │
        +3V3
```

### Critical Design Notes

- {Required cap values — min/max, type (MLCC vs electrolytic)}
- {Enable pin handling — float? tie high? external control?}
- {Thermal considerations — power dissipation, max ambient}
- {Any pins that MUST NOT be left floating}
- {Recommended PCB layout notes from datasheet}

### Datasheet Reference

- **Datasheet URL:** {url}
- **App circuit page/figure:** {page number or figure reference}
- **Key tables:** {relevant tables for component selection}

---

{Repeat for each IC / non-trivial component}

---

## Passive Component Sizing

| Component | Value  | Purpose               | How Value Was Determined                    |
|-----------|--------|-----------------------|---------------------------------------------|
| C1        | 1uF    | LDO input cap         | Datasheet Table X: min 0.7uF, using 1uF    |
| C2        | 1uF    | LDO output cap        | Datasheet Table X: min 0.7uF, using 1uF    |
| R1        | 4.7k   | I2C pull-up (SDA)     | Standard value for 3.3V I2C at 400kHz       |
| R2        | 1k     | Power LED resistor    | (3.3V - 2.2V) / 1mA = 1.1k, using 1k       |

## Net Plan

| Net Name  | Type    | Source              | Loads                              | Notes       |
|-----------|---------|---------------------|------------------------------------|-------------|
| +3V3      | Power   | U1.VOUT             | U2.VCC, R1, R2                     |             |
| GND       | Power   | Power symbol        | All component grounds              |             |
| SDA       | Signal  | J1.3                | U2.SDA, R1 pull-up                 | I2C data    |
| SCL       | Signal  | J1.4                | U2.SCL, R2 pull-up                 | I2C clock   |

---

**Status:** DRAFT — Awaiting user review of pinouts and application circuits

**Next step:** Once approved, proceed to Stage 5 (Schematic Generation)
