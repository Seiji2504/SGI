variable "aws_region" {
  type    = string
  default = "us-east-2"
}
variable "project_name" {
  type    = string
  default = "menoss"
}
variable "vpc_id" { type = string }
variable "private_subnet_ids" {
  type = list(string)
  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "RDS, ECS y el VPC Link requieren subnets privadas en al menos dos zonas."
  }
}
variable "frontend_origins" {
  type        = list(string)
  default     = []
  description = "Orígenes HTTPS exactos del frontend alojado fuera de AWS. Déjalo vacío hasta conocer la URL pública del frontend."
  validation {
    condition     = alltrue([for origin in var.frontend_origins : startswith(origin, "https://")])
    error_message = "Cada origen del frontend debe ser HTTPS, sin rutas ni barra final."

  }
}
variable "database_name" {
  type    = string
  default = "menoss"
}
variable "database_multi_az" {
  type    = bool
  default = true
}
variable "database_instance_class" {
  type    = string
  default = "db.t4g.medium"
}
variable "api_min_tasks" {
  type    = number
  default = 2
}
variable "api_max_tasks" {
  type    = number
  default = 10
}
variable "api_image_tag" {
  type    = string
  default = "latest"
}
variable "migration_image_tag" {
  type    = string
  default = "latest"
}
