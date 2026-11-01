variable "aws_region" { type = string
  default = "us-east-1" }
variable "project_name" { type = string
  default = "menoss" }
variable "vpc_id" { type = string }
variable "public_subnet_ids" { type = list(string)
  validation { condition = length(var.public_subnet_ids) >= 2
  error_message = "El ALB requiere subnets públicas en al menos dos zonas." } }
variable "private_subnet_ids" { type = list(string)
  validation { condition = length(var.private_subnet_ids) >= 2
  error_message = "RDS/ECS requieren subnets privadas en al menos dos zonas." } }
variable "certificate_arn" { type = string
  description = "ARN de un certificado ACM en la misma región que el ALB." }
variable "domain_name" { type = string
  description = "Nombre DNS público de la API que cubre el certificado ACM, por ejemplo api.midominio.pe." }
variable "route53_zone_id" { type = string
  description = "Hosted Zone de Route 53 para el dominio." }
variable "frontend_origins" {
  type = list(string)
  description = "Orígenes HTTPS exactos del frontend alojado fuera de AWS, por ejemplo [\"https://inventario.midominio.pe\"]. No incluir rutas ni barra final."
  validation {
    condition = length(var.frontend_origins) > 0 && alltrue([for origin in var.frontend_origins : startswith(origin, "https://")])
    error_message = "Configura al menos un origen HTTPS exacto para el frontend externo."
  }
}
variable "database_name" { type = string
  default = "menoss" }
variable "database_multi_az" { type = bool
  default = true }
variable "database_instance_class" { type = string
  default = "db.t4g.medium" }
variable "api_min_tasks" { type = number
  default = 2 }
variable "api_max_tasks" { type = number
  default = 10 }
variable "api_image_tag" { type = string
  default = "latest" }
variable "migration_image_tag" { type = string
  default = "latest" }
