aws_region   = "us-east-2"
project_name = "menoss"
vpc_id       = "vpc-0e51da06506b774cb"
private_subnet_ids = [
  "subnet-0dab97ed3b72abea9",
  "subnet-0fdaf2eb9cbf78a4c",
]
frontend_origins = ["https://unrivaled-mermaid-b583f0.netlify.app"]

database_multi_az       = true
database_instance_class = "db.t4g.medium"
api_min_tasks           = 2
api_max_tasks           = 10
api_image_tag       = "v1"
migration_image_tag = "v1"
