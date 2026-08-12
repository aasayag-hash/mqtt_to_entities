# Changelog

## 1.06

- Se agrega este changelog, así la lista de cambios aparece en el diálogo de actualización del add-on en Home Assistant.

## 1.05

- Filas de la tabla de entidades más compactas, para que entren más en pantalla.
- Los botones **Editar** y **Borrar** ya no se apilan: quedan en la misma línea.
- **Borrar** ahora es rojo, igual que en la tabla de brokers.

## 1.04

- **Redondeo de valores**: nueva opción en los mapeos de `sensor` y `number` (0 a 4 decimales). Un valor como `57.560001373291016` se puede guardar como `57.56` o `58`. Los payloads que no son numéricos pasan sin cambios.
- **Soporte para varios brokers a la vez**: cada broker mantiene su propia conexión, caché de topics, estado y reintentos.
  - La solapa **Conexión** ahora tiene el formulario a la izquierda y una planilla de brokers a la derecha, con estado, cantidad de topics y mensajes, y acciones de **Reconectar**, **Editar** y **Borrar** por fila.
  - El botón **Desconectar** fue reemplazado por **Agregar a brokers**.
  - Al editar un broker, si se deja la contraseña en blanco se conserva la guardada.
  - Los brokers se guardan en `/data/brokers.json` y se reconectan al reiniciar el add-on.
- Las entidades quedan asociadas a un broker, así el mismo topic en dos brokers distintos alimenta dos entidades separadas.
- La solapa **Explorar** tiene un selector de broker que controla el árbol de topics.
- Nueva columna **Broker** en la tabla de entidades.

## 1.03

- **Corregido**: las entidades quedaban en `null` sin explicación. Home Assistant rechaza entity IDs como `sensor de prueba` o `voltaje` (con espacios o sin dominio), y el error solo aparecía en el log del add-on.
  - Ahora el entity ID se valida al crear y editar, con un mensaje claro en el formulario.
  - Nueva columna **Estado** por entidad: `ok`, `esperando dato` o `error` (con el detalle del error).
  - El entity ID se sugiere automáticamente a partir del topic.
  - Al crear o editar un mapeo se aplica de inmediato el último payload recibido, sin esperar el próximo mensaje MQTT.
- **Unidad de medida** pasa a ser un desplegable con las unidades estándar de Home Assistant, agrupadas por magnitud, más una opción **Otra…** para unidades personalizadas.
- El panel de payload muestra cada campo con su valor actual y un botón **+ Crear entidad**.
- Buscador en la solapa **Entidades**.

## 1.02

- El árbol de topics ahora se despliega y se pliega por nodo, y arranca plegado.
- Contadores por nodo: cantidad de hijos y total de mensajes del subárbol.
- Vista previa del payload en los topics finales.
- Buscador sobre el árbol, que filtra por ruta de topic y por contenido del payload.
- Botón para plegar todo.
- El estado de conexión se muestra como etiqueta de color (verde al conectar) y el botón de conectar refleja la conexión activa.
- Botón **Desconectar**; se quitó **Reconectar**.

## 1.01

- **Corregido**: el botón **Conectar** no hacía nada. Las llamadas a la API usaban rutas absolutas y terminaban apuntando a la raíz de Home Assistant en lugar del prefijo de ingress del add-on, por lo que todas devolvían 404.

## 1.00

- Primera versión con numeración de release.
- Explorador de topics MQTT y mapeo de campos de payloads JSON a entidades de Home Assistant (`sensor`, `binary_sensor`, `switch`, `number`, `text`, `select`), sin YAML ni Jinja.
- Los valores de las entidades se restauran al reiniciar.
