# MQTT to Entities

Browse your MQTT broker's full topic tree, inspect retained JSON payloads, and
map any field (including array items, by index or by `field=value`) to a Home
Assistant entity — no YAML or Jinja required.

## Features

- Connects to its own MQTT broker (independent from Home Assistant's), configured
  from the **Conexión** tab.
- Subscribes to `#` and builds a live topic tree with the last payload per topic.
- **Explorar** tab: browse the tree, view JSON, click a field to create a mapping.
- **Entidades** tab: list, edit, and delete existing mappings.
- Supported entity domains: `sensor`, `binary_sensor`, `switch`, `number`, `text`, `select`.
- Mappings persist in `/data/mappings.json` and survive add-on restarts.

## Notes

- This add-on is Ingress-only; it does not expose a port to the LAN.
- Values are pushed via the Home Assistant Supervisor API
  (`/api/states/<entity_id>`), so entities behave as HA-managed states without
  a backing integration (they will not survive a full HA Core restart until a
  new MQTT message re-populates them).
