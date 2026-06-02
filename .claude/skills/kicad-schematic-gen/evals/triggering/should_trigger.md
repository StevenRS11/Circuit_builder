# Triggering corpus — SHOULD trigger `kicad-schematic-gen`

In-scope prompts. Each should activate the skill. `test_evals.py` checks (deterministically)
that every prompt is on-topic (shares a domain term with the SKILL.md `description:`). The
*actual* "did activation fire" check is a manual/assisted step — see `../README.md`.

One prompt per `- ` line:

- Design a 3S Li-ion battery charger board with USB-C PD input.
- Create a schematic for an LDO power supply that takes 5V in and outputs 3.3V.
- Build a breakout board for the BME280 environmental sensor.
- Make a USB-C PD trigger board using the CH224K to request 15V.
- Generate a KiCad schematic for a load-cell amplifier around the NAU7802.
- I need a buck converter power supply circuit for 12V to 5V at 2A.
- Design a sensor board around an I2C accelerometer with a 3.3V regulator.
- Lay out a battery charger using the TP4056 with a USB-C connector and status LEDs.
