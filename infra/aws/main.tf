locals {
  name = var.project_name
  common_tags = { Application = "Menoss", ManagedBy = "Terraform" }
}

resource "aws_ecr_repository" "api" {
  name = "${local.name}-api"
  image_scanning_configuration { scan_on_push = true }
  image_tag_mutability = "IMMUTABLE"
  tags = local.common_tags
}
resource "aws_ecr_repository" "migration" {
  name = "${local.name}-migration"
  image_scanning_configuration { scan_on_push = true }
  image_tag_mutability = "IMMUTABLE"
  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "api" { name = "/ecs/${local.name}/api"
  retention_in_days = 30
  tags = local.common_tags }
resource "aws_security_group" "alb" {
  name_prefix = "${local.name}-alb-"
  description = "Public HTTPS entry point"
  vpc_id = var.vpc_id
  ingress { from_port = 443
  to_port = 443
  protocol = "tcp"
  cidr_blocks = ["0.0.0.0/0"] }
  ingress { from_port = 80
  to_port = 80
  protocol = "tcp"
  cidr_blocks = ["0.0.0.0/0"] }
  egress { from_port = 0
  to_port = 0
  protocol = "-1"
  cidr_blocks = ["0.0.0.0/0"] }
  tags = local.common_tags
}
resource "aws_security_group" "tasks" {
  name_prefix = "${local.name}-tasks-"
  description = "Menoss ECS tasks"
  vpc_id = var.vpc_id
  ingress { from_port = 8080
  to_port = 8080
  protocol = "tcp"
  security_groups = [aws_security_group.alb.id] }
  egress { from_port = 0
  to_port = 0
  protocol = "-1"
  cidr_blocks = ["0.0.0.0/0"] }
  tags = local.common_tags
}
resource "aws_security_group" "database" {
  name_prefix = "${local.name}-db-"
  description = "PostgreSQL accepts traffic only from ECS tasks"
  vpc_id = var.vpc_id
  ingress { from_port = 5432
  to_port = 5432
  protocol = "tcp"
  security_groups = [aws_security_group.tasks.id] }
  tags = local.common_tags
}

resource "aws_db_subnet_group" "main" {
  name = "${local.name}-db"
  subnet_ids = var.private_subnet_ids
  tags = local.common_tags
}
resource "random_password" "db_app" { length = 40
  special = false }
resource "aws_secretsmanager_secret" "db_app" {
  name = "${local.name}/database/app-credentials"
  description = "Restricted Menoss runtime database account"
  tags = local.common_tags
}
resource "aws_secretsmanager_secret_version" "db_app" {
  secret_id = aws_secretsmanager_secret.db_app.id
  secret_string = jsonencode({ username = "menoss_app", password = random_password.db_app.result })
}
resource "aws_db_instance" "main" {
  identifier = "${local.name}-postgres"
  engine = "postgres"
  instance_class = var.database_instance_class
  allocated_storage = 30
  max_allocated_storage = 200
  storage_type = "gp3"
  storage_encrypted = true
  db_name = var.database_name
  username = "menoss_admin"
  manage_master_user_password = true
  db_subnet_group_name = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]
  multi_az = var.database_multi_az
  publicly_accessible = false
  backup_retention_period = 14
  deletion_protection = true
  copy_tags_to_snapshot = true
  auto_minor_version_upgrade = true
  skip_final_snapshot = false
  final_snapshot_identifier = "${local.name}-final"
  tags = local.common_tags
}

resource "aws_cognito_user_pool" "main" {
  name = "${local.name}-users"
  username_attributes = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection = "ACTIVE"
  password_policy {
    minimum_length = 8
    require_lowercase = true
    require_numbers = true
    require_symbols = true
    require_uppercase = true
    temporary_password_validity_days = 7
  }
  schema { name = "name"
  attribute_data_type = "String"
  mutable = true
  required = true
  string_attribute_constraints { min_length = 1
  max_length = 120 } }
  tags = local.common_tags
}
resource "aws_cognito_user_pool_client" "web" {
  name = "${local.name}-web"
  user_pool_id = aws_cognito_user_pool.main.id
  generate_secret = false
  explicit_auth_flows = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  access_token_validity = 1
  id_token_validity = 1
  refresh_token_validity = 30
  token_validity_units { access_token = "hours"
  id_token = "hours"
  refresh_token = "days" }
  read_attributes = ["email", "name", "email_verified"]
  write_attributes = ["email", "name"]
}

resource "aws_lb" "main" {
  name = "${local.name}-alb"
  load_balancer_type = "application"
  internal = false
  subnets = var.public_subnet_ids
  security_groups = [aws_security_group.alb.id]
  enable_deletion_protection = true
  tags = local.common_tags
}
resource "aws_route53_record" "app" {
  zone_id = var.route53_zone_id
  name = var.domain_name
  type = "A"
  alias {
    name = aws_lb.main.dns_name
    zone_id = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
resource "aws_lb_target_group" "api" {
  name = "${local.name}-api"
  port = 8080
  protocol = "HTTP"
  vpc_id = var.vpc_id
  target_type = "ip"
  health_check { path = "/health"
  matcher = "200"
  interval = 30 }
  tags = local.common_tags
}
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port = 80
  protocol = "HTTP"
  default_action { type = "redirect"
  redirect { port = "443"
  protocol = "HTTPS"
  status_code = "HTTP_301" } }
}
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port = 443
  protocol = "HTTPS"
  certificate_arn = var.certificate_arn
  ssl_policy = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  default_action { type = "forward"
  target_group_arn = aws_lb_target_group.api.arn }
}

data "aws_iam_policy_document" "ecs_task_assume" {
  statement { actions = ["sts:AssumeRole"]
  principals { type = "Service"
  identifiers = ["ecs-tasks.amazonaws.com"] } }
}
resource "aws_iam_role" "task_execution" {
  name = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume.json
  tags = local.common_tags
}
resource "aws_iam_role" "task_runtime" {
  name = "${local.name}-ecs-runtime"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume.json
  tags = local.common_tags
}
resource "aws_iam_role_policy" "ecs_exec_channels" {
  name = "ecs-exec-channels"
  role = aws_iam_role.task_runtime.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Action = ["ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel"],
      Resource = "*"
    }]
  })
}
resource "aws_iam_role_policy_attachment" "execution_managed" {
  role = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "read_db_secret" {
  name = "read-runtime-db-secret"
  role = aws_iam_role.task_execution.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [aws_secretsmanager_secret.db_app.arn] }] })
}
resource "aws_iam_role" "migration_execution" {
  name = "${local.name}-migration-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume.json
  tags = local.common_tags
}
resource "aws_iam_role_policy_attachment" "migration_execution_managed" {
  role = aws_iam_role.migration_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "read_migration_secrets" {
  name = "read-database-bootstrap-secrets"
  role = aws_iam_role.migration_execution.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [aws_secretsmanager_secret.db_app.arn, aws_db_instance.main.master_user_secret[0].secret_arn] }] })
}
resource "aws_ecs_cluster" "main" {
  name = "${local.name}-cluster"
  setting { name = "containerInsights"
  value = "enabled" }
  tags = local.common_tags
}

resource "aws_ecs_task_definition" "api" {
  family = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode = "awsvpc"
  cpu = "512"
  memory = "1024"
  execution_role_arn = aws_iam_role.task_execution.arn
  task_role_arn = aws_iam_role.task_runtime.arn
  container_definitions = jsonencode([{
    name = "api", image = "${aws_ecr_repository.api.repository_url}:${var.api_image_tag}", essential = true,
    portMappings = [{ containerPort = 8080, protocol = "tcp" }],
    environment = [
      { name = "PORT", value = "8080" }, { name = "DB_HOST", value = aws_db_instance.main.address },
      { name = "DB_PORT", value = "5432" }, { name = "DB_NAME", value = var.database_name },
      { name = "DB_USER", value = "menoss_app" }, { name = "DB_SSLMODE", value = "require" },
      { name = "DB_POOL_MIN_SIZE", value = "1" }, { name = "DB_POOL_MAX_SIZE", value = "5" },
      { name = "COGNITO_REGION", value = var.aws_region }, { name = "COGNITO_USER_POOL_ID", value = aws_cognito_user_pool.main.id },
      { name = "COGNITO_CLIENT_ID", value = aws_cognito_user_pool_client.web.id },
      { name = "CORS_ORIGINS", value = join(",", var.frontend_origins) }
    ],
    secrets = [{ name = "DB_PASSWORD", valueFrom = "${aws_secretsmanager_secret.db_app.arn}:password::" }],
    healthCheck = { command = ["CMD-SHELL", "python -c 'import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:8080/health\", timeout=3)'"], interval = 30, timeout = 5, retries = 3, startPeriod = 30 },
    logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.api.name, "awslogs-region" = var.aws_region, "awslogs-stream-prefix" = "api" } }
  }])
  tags = local.common_tags
}
resource "aws_ecs_task_definition" "migration" {
  family = "${local.name}-migration"
  requires_compatibilities = ["FARGATE"]
  network_mode = "awsvpc"
  cpu = "256"
  memory = "512"
  execution_role_arn = aws_iam_role.migration_execution.arn
  task_role_arn = aws_iam_role.task_runtime.arn
  container_definitions = jsonencode([{
    name = "migration", image = "${aws_ecr_repository.migration.repository_url}:${var.migration_image_tag}", essential = true,
    environment = [
      { name = "PGHOST", value = aws_db_instance.main.address }, { name = "PGPORT", value = "5432" },
      { name = "PGDATABASE", value = var.database_name }, { name = "PGUSER", value = "menoss_admin" },
      { name = "PGSSLMODE", value = "require" }
    ],
    secrets = [
      { name = "PGPASSWORD", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::" },
      { name = "APP_PASSWORD", valueFrom = "${aws_secretsmanager_secret.db_app.arn}:password::" }
    ],
    logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.api.name, "awslogs-region" = var.aws_region, "awslogs-stream-prefix" = "migration" } }
  }])
  depends_on = [aws_iam_role_policy_attachment.migration_execution_managed, aws_iam_role_policy.read_migration_secrets]
  tags = local.common_tags
}

resource "aws_ecs_service" "api" {
  name = "${local.name}-api"
  cluster = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count = var.api_min_tasks
  launch_type = "FARGATE"
  platform_version = "LATEST"
  enable_execute_command = true
  deployment_circuit_breaker { enable = true
  rollback = true }
  network_configuration { subnets = var.private_subnet_ids
  security_groups = [aws_security_group.tasks.id]
  assign_public_ip = false }
  load_balancer { target_group_arn = aws_lb_target_group.api.arn
  container_name = "api"
  container_port = 8080 }
  depends_on = [aws_lb_listener.https, aws_iam_role_policy_attachment.execution_managed, aws_iam_role_policy.read_db_secret, aws_iam_role_policy.ecs_exec_channels]
  tags = local.common_tags
}

resource "aws_appautoscaling_target" "api" {
  max_capacity = var.api_max_tasks
  min_capacity = var.api_min_tasks
  resource_id = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace = "ecs"
}
resource "aws_appautoscaling_policy" "api_cpu" {
  name = "${local.name}-api-cpu"
  policy_type = "TargetTrackingScaling"
  resource_id = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  service_namespace = aws_appautoscaling_target.api.service_namespace
  target_tracking_scaling_policy_configuration { target_value = 60
  predefined_metric_specification { predefined_metric_type = "ECSServiceAverageCPUUtilization" }
  scale_in_cooldown = 180
  scale_out_cooldown = 60 }
}
