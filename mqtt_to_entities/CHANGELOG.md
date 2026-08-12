# Changelog

## 1.14

Correcciones encontradas en una revisión del código, incluidas dos que introdujo la versión 1.12:

- **Corregido (importante)**: la configuración de los brokers (dirección, usuario y contraseña) se guardaba de una forma que podía perderse por completo si el add-on se cortaba justo durante la escritura, dejando también las entidades huérfanas. Ahora se guarda con el mismo método a prueba de cortes que ya usaban las entidades, y si el archivo llegara a estar dañado se conserva una copia en lugar de sobrescribirlo.
- **Corregido**: el guardado periódico que agregó la 1.12 reescribía el archivo completo cada 30 segundos **aunque no hubiera ningún cambio**, incluso con el add-on sin recibir un solo mensaje. En una Raspberry Pi eso era desgaste de la tarjeta SD a cambio de nada. Ahora sólo escribe cuando hay algo nuevo.
- **Corregido**: al guardar en disco, el add-on dejaba de procesar mensajes MQTT durante toda la escritura. Con una tarjeta SD lenta eso eran unos 260 milisegundos de pausa por guardado (alrededor de 130 mensajes demorados). Ahora la escritura ocurre por fuera y la pausa es de menos de 1 milisegundo.
- **Corregido**: si se editaban dos brokers al mismo tiempo apuntándolos a la misma dirección, ambos podían aceptarse y quedaban dos conexiones al mismo servidor. Era el mismo problema que la 1.12 corrigió al **crear** un broker, que seguía presente al **editarlo**.

## 1.13

- **Limpieza interna**: al rechazar el alta de un broker repetido, ya no se arma el objeto de conexión que luego se descartaba. No cambia nada de lo que se ve ni corrige ninguna falla; deja el código preparado para que eso no se convierta en un problema más adelante.

## 1.12

Los cuatro hallazgos menores que quedaban del review anterior, ya resueltos:

- **Corregido**: si el add-on se reiniciaba muy seguido (menos de 5 minutos entre reinicios), el reloj que detecta topics sin datos nunca llegaba a cumplirse y las entidades nunca pasaban a `unknown` aunque el dispositivo estuviera realmente desconectado. Ahora ese dato se guarda en disco cada 30 segundos en lugar de solo al apagar el add-on de forma prolija.
- **Corregido**: al editar la configuración avanzada de una entidad (por ejemplo el redondeo), el valor guardado podía compartirse por accidente con la copia mostrada en la web. No causaba errores hoy, pero era un riesgo latente. Ahora cada copia es independiente.
- **Corregido**: si se enviaban dos altas de broker al mismo tiempo para la misma dirección, ambas podían crear una conexión duplicada. Ahora la verificación es atómica y sólo una prospera.
- **Corregido**: el contador de mensajes por rama del árbol de topics podía quedar por debajo del real después de que un topic muy antiguo se descartara de la memoria y luego volviera a aparecer. Ahora el total se sigue contando bien.

## 1.11

Correcciones a problemas introducidos por las versiones 1.07 y 1.08:

- **Corregido (importante)**: si la respuesta del broker llegaba justo cuando expiraba el tiempo de espera de un reintento, el broker quedaba marcado como "error" aunque estuviera conectado y recibiendo datos. Eso hacía que sus entidades pasaran a `unknown` con datos llegando, y que el siguiente reintento cortara una conexión que funcionaba bien.
- **Corregido**: al perder la conexión, las actualizaciones a Home Assistant se hacían dentro del hilo de red de MQTT y bloqueando la gestión de conexiones. Con muchas entidades y Home Assistant lento, esto podía frenar la reconexión durante minutos. Ahora ese trabajo se hace en un proceso aparte y el hilo de red responde de inmediato.
- **Corregido**: si Home Assistant no respondía al marcar una entidad como desconocida, el intento se repetía en cada cambio de estado del broker, indefinidamente. Ahora se intenta una vez y se reintenta recién cuando llegan datos nuevos.

## 1.10

- **Topics `$SYS` del broker**: el comodín `#` no incluye los topics que empiezan con `$` (así lo define el estándar MQTT), por lo que las estadísticas internas del broker no se veían. Ahora hay una casilla **Suscribir a `$SYS`** en el formulario de cada broker, desactivada por defecto para no llenar el árbol con topics de diagnóstico. Los brokers que la tengan activa se marcan con `+$SYS` en la planilla.
- Nota: no hace falta suscribir a `/#` por separado, porque ya está incluido en `#`.

## 1.09

- **Topics que no publican JSON**: antes, si un topic enviaba un valor suelto (por ejemplo `52.3` en lugar de `{"value":52.3}`), la entidad quedaba sin datos para siempre y sin ninguna explicación. Ahora se puede mapear el valor completo del topic y funciona.
- **Mensajes de error claros al mapear**: si el campo elegido no está en el payload, la entidad indica cuáles son los campos disponibles. Si el valor recibido es nulo, también lo informa.
- **Memoria acotada**: la lista de topics que el add-on mantiene en memoria ahora tiene un límite (20.000 por broker) y descarta los menos usados, para que un broker con muchísimos topics distintos no haga crecer la memoria sin control. Los payloads muy grandes se recortan para la vista previa. Las entidades ya creadas no se ven afectadas; la tabla de brokers avisa si se descartó algo.
- **Corregido**: al hacer clic rápido en varios topics del árbol, o al cambiar de broker mientras cargaba, podía quedar en pantalla el payload de un topic mientras la aplicación creía estar en otro, y la entidad se creaba sobre el topic equivocado.
- Si un topic todavía no tiene datos, el panel lo indica en lugar de quedar en blanco.

## 1.08

- **Entidades en desconocido cuando un topic deja de publicar**: además del caso en que se cae el broker, ahora cada entidad pasa a `unknown` si su topic deja de enviar datos, aunque el broker siga conectado (por ejemplo, un equipo que se desconecta del bus).
- Nuevo campo **Timeout sin datos** al crear o editar una entidad, con opciones de 1 minuto a 1 día, o **Nunca** para desactivarlo. Por defecto son **5 minutos**.
- La entidad vuelve a mostrar su valor en cuanto llega un dato nuevo.
- En la tabla de entidades, las que están sin datos se muestran como **desconocido** en lugar de mostrar el texto `unknown`.

## 1.07

Correcciones a partir de una revisión del código:

- **Corregido**: un corte de luz o un reinicio forzado a mitad de escritura podía dejar `mappings.json` corrupto, y en ese caso el add-on quedaba inservible de forma permanente (todos los endpoints devolvían error y no se podía ver ni editar ninguna entidad). Ahora las escrituras son atómicas y, si el archivo estuviera dañado, el add-on arranca igual y preserva una copia del archivo original.
- **Corregido**: si se abría una entidad con **Editar**, se cancelaba, y después se creaba una entidad nueva, la nueva no se creaba y en su lugar se sobrescribía la que se había abierto antes.
- **Corregido**: al crear un mapeo nuevo, el formulario heredaba los valores de configuración del mapeo anterior (valores ON/OFF, mínimo, máximo, opciones).
- **Rendimiento**: cada mensaje MQTT leía el archivo de entidades completo del disco, incluso los mensajes de topics sin ninguna entidad asociada (la enorme mayoría). En un broker con mucho tráfico esto castigaba la tarjeta SD y trababa la interfaz. Ahora la lista se mantiene en memoria y los valores se guardan en disco de forma agrupada.
- **Reconexión indefinida**: un broker que se caía podía quedar en "desconectado" para siempre, sin volver a intentar. Ahora reintenta indefinidamente, con espera progresiva, hasta que el broker vuelva o se lo quite de la lista. También reintenta cuando el broker rechaza la conexión (por ejemplo, credenciales incorrectas), porque suele ser algo que se corrige del lado del broker.
- **Entidades en desconocido al perder conexión**: cuando se corta la conexión con un broker, sus entidades pasan a `unknown` en Home Assistant en lugar de seguir mostrando el último valor recibido como si fuera actual. Al volver la conexión, se actualizan con el primer dato nuevo. Los valores guardados se siguen restaurando al reiniciar el add-on.
- El estado del broker ahora refleja el fallo y el motivo mientras reintenta, en lugar de quedar en "conectando".
- El título del formulario de entidad distingue entre **Nuevo mapeo** y **Editar mapeo**.

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
