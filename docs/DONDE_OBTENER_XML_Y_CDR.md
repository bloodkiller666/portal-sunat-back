# ¿A quién pedirle el XML y el CDR? (guía para TI)

Documento para quien desarrolla el portal (TI) y **no administra** la facturación
electrónica de la empresa. Explica quién tiene los XML y CDR, cómo descubrirlo y qué
pedirle, sin tener que saber de contabilidad.

## 1. El problema en una frase

El portal ya lista los comprobantes (vía SIRE de SUNAT), pero SIRE **solo entrega los
datos del registro de ventas**, no el XML firmado ni el CDR. Tampoco `getStatusCdr` los
entrega para facturas emitidas por otro sistema (responde `0013`, "no pertenece a GEM").
Por eso el XML y el CDR hay que pedírselos a **quien emitió la factura**.

> Regla simple: el XML y el CDR los tiene quien **emite y envía** el comprobante a SUNAT.
> SUNAT solo los muestra por pantalla (SOL) a una persona, no por una API abierta.

## 2. ¿Quién los tiene? (de más a menos probable)

| # | Quién | Qué tiene | Cómo se llama en la empresa |
|---|-------|-----------|-----------------------------|
| 1 | **Proveedor OSE/PSE** (si la empresa usa uno) | XML firmado, CDR y PDF de cada comprobante; casi siempre con API o descarga masiva | Un software externo (ej. "facturador web") |
| 2 | **Equipo SAP / TI corporativo** (si se factura desde SAP) | Los XML/CDR que SAP genera o recibe de su conector | Consultor SAP, ABAP, Basis, "Facturación electrónica SAP" |
| 3 | **Contabilidad / Facturación** | Saben **qué sistema** se usa y quién es el contacto con el proveedor; suelen tener los archivos descargados | Contador, jefe de facturación, analista de créditos |
| 4 | **Quien tiene la Clave SOL** | Puede descargar manualmente XML/CDR desde SUNAT (SOL) | Contador o administrador, **no TI** |

Orden recomendado para preguntar: **Contabilidad → (te dicen el sistema) → Proveedor OSE/PSE
o equipo SAP**. Contabilidad casi siempre sabe el nombre del proveedor y tiene el contacto.

## 3. Cómo averiguar el proveedor sin preguntar a nadie

Mira una factura emitida reciente (el PDF que reciben los clientes):

- **Pie de página** del PDF: suele decir "Representación impresa de la factura electrónica…
  Autorizado mediante Resolución… consulte en **www.xxxxx.com**". Ese dominio es el
  proveedor (PSE/OSE).
- **El XML** (si tienes uno de prueba): en la firma digital `<ds:Signature>` y en
  `cbc:ProfileID`/notas suele aparecer el software.
- **Correo con el que llegan las facturas** a los clientes: el remitente delata el sistema.
- **SUNAT SOL → Facturación electrónica**: ahí se ve con qué sistema está afiliada la
  empresa (esto lo ve quien tenga la Clave SOL; confírmalo con Contabilidad).

## 4. Qué pedirle (según a quién)

### Si es un proveedor OSE/PSE

Pídele por escrito (es un servicio comercial, normalmente con contrato):

1. **Documentación de su API** (o del servicio de descarga masiva) para consultar por
   `RUC + tipo + serie + número` y obtener **XML, CDR y PDF**.
2. **Credenciales de integración** (token/usuario de API) para la empresa RUC
   `20514831620`, de solo lectura si se puede.
3. **Límites**: peticiones por minuto/día, tamaño de descarga, si hay costo por consulta
   o por documento, y **cuánto tiempo conservan** los documentos (retención).
4. **Entorno de pruebas** (sandbox) para desarrollar sin tocar producción.
5. Si no tienen API: ¿ofrecen **exportación masiva** (ZIP) o **webhook/correo** de cada
   comprobante emitido?

### Si es SAP / TI corporativo

1. ¿Dónde guarda SAP el XML y el CDR? (tabla, carpeta compartida, DMS, conector de
   facturación electrónica).
2. ¿Pueden **exponer una API/servicio (OData, RFC, REST)** o dejar los archivos en una
   carpeta o bucket que el portal pueda leer?
3. Un usuario técnico **solo lectura** y el **volumen** (documentos por mes).

### Si lo tiene Contabilidad (manual, sin API)

Pídeles una descarga periódica (ZIP de XML y ZIP de CDR). El portal **ya** sabe
cargarlos masivamente, sin desarrollo nuevo:

- XML (+ PDF): `POST /companies/{ruc}/comprobantes/import-paquete` (ZIP de hasta 200 archivos).
- CDR: `POST /companies/{ruc}/comprobantes/import-cdr` (ZIP con nombres `R-RUC-tipo-serie-número`).

## 5. Mensaje listo para enviar

> Asunto: Solicitud de acceso a XML y CDR de comprobantes electrónicos – RUC 20514831620
>
> Hola, soy [tu nombre] de TI. Estoy desarrollando el portal interno de comprobantes
> electrónicos (consulta y respaldo de facturas, boletas y notas). El portal ya obtiene el
> listado desde SUNAT (SIRE), pero necesito también el **XML firmado y el CDR** de cada
> comprobante para respaldo y auditoría.
>
> ¿Me pueden indicar **con qué sistema/proveedor emitimos** los comprobantes (OSE/PSE o SAP) y
> ponerme en contacto con quien lo administre? Necesito saber si ofrece **API o descarga
> masiva**, el **entorno de pruebas**, los **límites y costos**, y cuánto tiempo conserva
> los archivos.
>
> Gracias.

## 6. Qué recibirá el portal y cómo se integra

Con cualquiera de las opciones, el portal solo necesita una función:

```
obtener(ruc, tipo, serie, numero) -> { xml, cdr, pdf? }
```

El resto ya existe: los archivos se guardan en el bucket privado (R2), el comprobante queda
con `tiene_xml/tiene_cdr` y el XML completa los ítems de lo que vino de SIRE
(`completar_desde_xml`). En cuanto sepas el proveedor, se agrega un adaptador pequeño en
`app/integrations/` (uno por proveedor) y se programa una sincronización.

## 7. Lo que NO conviene hacer

- **Scraping de SUNAT SOL o automatizar la Clave SOL**: viola los términos, se rompe con
  cualquier cambio y arriesga bloquear la clave de la empresa. El proyecto solo usa
  servicios oficiales.
- **Pedirle la Clave SOL a Contabilidad para TI**: no hace falta; basta con las
  credenciales de API del proveedor o con los archivos.
- **Regenerar el XML en el portal**: el único válido es el firmado que se envió a SUNAT.

## 8. Checklist rápido

- [ ] Ver el pie de un PDF de factura reciente para identificar al proveedor.
- [ ] Preguntar a Contabilidad: ¿qué sistema, quién es el contacto?
- [ ] Enviar el mensaje de la sección 5 al contacto (proveedor o SAP).
- [ ] Obtener: documentación de API, credenciales de prueba, límites, costos, retención.
- [ ] Mientras tanto: pedir a Contabilidad un ZIP mensual y cargarlo con `import-paquete` /
      `import-cdr`.
