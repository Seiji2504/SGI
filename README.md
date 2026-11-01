# Menoss · Gestión de inventario

Aplicación de inventario con React + Vite, API FastAPI, PostgreSQL y Amazon Cognito. La infraestructura de referencia usa Amazon ECS Fargate, Application Load Balancer, ECR, RDS PostgreSQL, Secrets Manager y CloudWatch.

## Ejecutar en local con Docker

Requiere Docker Engine y Docker Compose. El Compose local inicia PostgreSQL y aplica `database/schema.sql` al crear su volumen por primera vez.

1. Copia `.env.example` a `.env` en la raíz y configura `COGNITO_REGION`, `COGNITO_USER_POOL_ID` y `COGNITO_CLIENT_ID` (los valores están en Terraform outputs; primero hay que crear el User Pool).
2. Ejecuta desde la raíz:

   ```bash
   docker compose up --build
   ```

3. Abre `http://localhost:5173`; la API está en `http://localhost:8000` y su documentación en `/docs`.

El login y el registro locales usan el User Pool real de Cognito. Sin esos tres valores, el frontend o la API no podrán autenticar usuarios. Para borrar la base local y volver a aplicar el esquema desde cero, ejecuta `docker compose down -v` (esto elimina los datos locales).

## Base de datos y autenticación

- `database/schema.sql` es el esquema de PostgreSQL para AWS. Define usuarios, bodegas, membresías con roles, productos, stock y movimientos; incluye índices para búsqueda/paginación y políticas RLS que limitan cada transacción al usuario y bodega activos.
- `database/bootstrap.sql` aplica el esquema y crea el rol SQL de runtime con permisos limitados. Se ejecuta desde una tarea efímera de ECS.
- Cognito autentica cuentas. FastAPI verifica JWT localmente con el JWKS de Cognito cacheado durante una hora; no hace una petición a Cognito por cada API request.
- La cuenta que inicia sesión por primera vez recibe una bodega Menoss propia. Las rutas `/api/warehouses` y `/api/warehouses/{id}/members` permiten consultar/crear bodegas y añadir cuentas existentes por su `user_id` de Cognito. Los roles `owner`, `admin`, `member` pueden modificar; `viewer` solo consulta.
- La aplicación archiva productos al eliminarlos para conservar el historial de movimientos.

`supabase/schema.sql` queda como referencia de la versión anterior. La aplicación nueva ya no depende de Supabase. Los usuarios y datos que ya existan allí no se copian automáticamente: antes del cambio productivo hay que exportarlos y migrarlos, y las cuentas deben crearse o migrarse a Cognito.

## Despliegue de referencia en AWS

La plantilla está en `infra/aws/` y despliega **solo la API y su infraestructura**: ECS Fargate, ALB, ECR, RDS PostgreSQL, Cognito, Secrets Manager y CloudWatch. El frontend se aloja fuera de AWS. Requiere AWS CLI autenticado, Terraform 1.10+, Docker, `jq`, una VPC existente, dos subnets públicas, dos privadas con salida a los servicios de AWS (NAT o endpoints), un dominio alojado en Route 53 y un certificado ACM para el dominio de la API en la región del ALB. Las subnets privadas alojan ECS y RDS; el ALB recibe HTTPS y redirige HTTP a HTTPS.

1. Copia `infra/aws/terraform.tfvars.example` a `infra/aws/terraform.tfvars` y completa VPC, subnets, ARN ACM, dominio de la API (`api.midominio.pe`), Hosted Zone y el origen HTTPS exacto del frontend externo (`https://inventario.midominio.pe`, sin ruta ni barra final). El dominio API debe estar cubierto por el certificado ACM. Configura primero el backend state remoto: copia `backend.tf.example` como `backend.tf` y completa el bucket S3 cifrado con versionado antes de `terraform init`. El state contiene credenciales generadas.
2. Inicializa Terraform y crea primero los registros ECR y el User Pool para conocer el Cognito client ID:

   ```bash
   cd infra/aws
   terraform init
   terraform apply -target=aws_ecr_repository.api -target=aws_ecr_repository.migration -target=aws_cognito_user_pool.main -target=aws_cognito_user_pool_client.web
   ```

3. Construye y publica las imágenes de la API y de migración con una etiqueta única (`v1` en este ejemplo). No se crea ni se sube una imagen del frontend. Sustituye `ACCOUNT_ID` por tu cuenta y conserva la misma región que Terraform:

   ```bash
   REGION=us-east-2
   ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
   API_REPO=$(terraform output -raw api_ecr_repository)
   MIGRATION_REPO=$(terraform output -raw migration_ecr_repository)
   aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"
   docker build -t "$API_REPO:v1" ./../../backend
   docker build -t "$MIGRATION_REPO:v1" -f ../../database/Dockerfile ../../database
   docker push "$API_REPO:v1"
   docker push "$MIGRATION_REPO:v1"
   ```

4. Crea la infraestructura con esas etiquetas y despliega la API:

   ```bash
   terraform apply -var='api_image_tag=v1' -var='migration_image_tag=v1'
   ```

5. Ejecuta la migración inicial en una tarea efímera dentro de ECS. La tarea usa la credencial maestra gestionada por RDS solo para inicializar el esquema y el rol limitado de la API. Espera a que la tarea termine con `exitCode: 0` en ECS/CloudWatch antes de usar la aplicación:

   ```bash
   cd infra/aws
   CLUSTER=$(terraform output -raw ecs_cluster_name)
   TASK=$(terraform output -raw migration_task_definition)
   TASK_SG=$(terraform output -raw ecs_task_security_group_id)
   SUBNETS=$(terraform output -json private_subnet_ids | jq -r 'join(",")')
   aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE --task-definition "$TASK" \
     --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$TASK_SG],assignPublicIp=DISABLED}"
   ```

6. La URL pública de la API aparece como output `api_url`. Comprueba `/health` y abre `/docs` para verificarla:

   ```bash
   terraform output -raw api_url
   ```

7. En el proveedor donde alojes el frontend, configura `VITE_API_URL` con ese `api_url`, `VITE_COGNITO_REGION` con la región del User Pool y `VITE_COGNITO_CLIENT_ID` con el output `cognito_client_id`. El valor `frontend_origins` de `terraform.tfvars` debe coincidir exactamente con el dominio HTTPS del frontend. Si cambia el dominio, actualiza esa variable y aplica Terraform de nuevo.

La plantilla establece dos tareas iniciales para la API, escalables por CPU hasta el límite configurado; usa RDS cifrado, backups de 14 días y Multi-AZ por defecto. Ajusta `infra/aws/terraform.tfvars` según el tráfico y el presupuesto. La plantilla no crea la VPC, subnets, NAT, dominio ni certificado ACM. No ejecutes Terraform en producción hasta revisar su plan, costos, state remoto y proceso de restauración.

Para publicar una versión posterior, construye imágenes con una etiqueta nueva (por ejemplo `v2`), súbelas a ECR y aplica Terraform con `api_image_tag` y `migration_image_tag` actualizados. ECR bloquea la sobrescritura de tags.

## Escalabilidad incluida

- `GET /api/products` aplica búsqueda, categoría, stock bajo y paginación (`page`, `page_size`, máximo 100) en PostgreSQL; devuelve `items`, `total` y `pages`.
- La API calcula resumen y categorías en SQL; no recorre todo el catálogo en Python.
- FastAPI verifica JWT Cognito usando JWKS cacheado; cada tarea tiene un pool DB acotado (`DB_POOL_MAX_SIZE=5`). Mantén el máximo de conexiones agregado por número máximo de tareas por debajo de la capacidad de RDS; considera RDS Proxy si ese crecimiento requiere más conexiones.
- Crear/editar productos y registrar el stock inicial/movimientos se ejecutan en transacciones PostgreSQL. Las modificaciones concurrentes del stock bloquean la fila de detalle.
- ECS Fargate escala automáticamente por CPU; el ALB distribuye tráfico y hace health checks; CloudWatch conserva logs 30 días.
- El modelo multi-bodega separa existencias por `warehouse_id`; memberships y RLS validan acceso y permisos en cada transacción.
