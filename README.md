# mqtt_to_entities

Repositorio de add-ons de Home Assistant.

## Instalación

1. En Home Assistant: **Configuración → Add-ons → Tienda de add-ons**.
2. Menú (⋮) superior derecho → **Repositorios**.
3. Agregar: `https://github.com/aasayag-hash/mqtt_to_entities`
4. Instalar el add-on **MQTT to Entities**.

## Add-ons incluidos

- [`mqtt_to_entities`](mqtt_to_entities/README.md) — explora el árbol de topics de un broker MQTT y mapea campos de payloads JSON (incluyendo campos dentro de arrays) al estado de entidades de Home Assistant (`sensor`, `binary_sensor`, `switch`, `number`, `text`, `select`), sin YAML ni Jinja.
