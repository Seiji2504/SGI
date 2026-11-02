terraform {
  backend "s3" {
    bucket       = "menoss-terraform-state-2524"
    key          = "menoss/aws/terraform.tfstate"
    region       = "us-east-2"
    encrypt      = true
    use_lockfile = true
  }
}
