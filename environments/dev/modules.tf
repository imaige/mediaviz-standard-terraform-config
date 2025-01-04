# Existing Infrastructure
module "vpc" {
  source = "./../../modules/networking"

  cluster_name = var.cluster_name
  env          = var.env
}

module "eks" {
  source = "./../../modules/eks"

  cluster_name    = var.cluster_name
  env             = var.env
  cluster_version = var.cluster_version

  vpc_id                   = module.vpc.vpc_id
  subnet_ids               = module.vpc.private_subnets
  control_plane_subnet_ids = module.vpc.public_subnets

  eks_primary_instance_type = var.eks_primary_instance_type
  node_group_min_size       = var.node_group_min_size
  node_group_max_size       = var.node_group_max_size
  node_group_desired_size   = var.node_group_desired_size

  aws_account_id = var.aws_account_id
  kms_key_arn    = module.security.kms_key_arn
  kms_key_id     = module.security.kms_key_id
}

# New Serverless Infrastructure
module "s3" {
  source = "./../../modules/s3"

  project_name         = var.project_name
  env                  = var.env
  cors_allowed_origins = var.cors_allowed_origins
  retention_days       = var.retention_days
  kms_key_arn          = module.security.kms_key_arn
  kms_key_id           = module.security.kms_key_id
  replica_kms_key_id   = module.security.kms_key_id
}

module "lambda" {
  source = "./../../modules/lambda"

  project_name                = var.project_name
  env                         = var.env
  s3_bucket_name              = module.s3.bucket_id
  s3_bucket_arn               = module.s3.bucket_arn
  tags                        = var.tags
  signing_profile_version_arn = module.security.signing_profile_arn
  subnet_ids                  = module.vpc.private_subnets
  vpc_id                      = module.vpc.vpc_id
  kms_key_arn                 = module.security.kms_key_arn
  kms_key_id                  = module.security.kms_key_id
  # encrypted_env_var           = var.encrypted_env_var
  sqs_queue_arn            = module.sqs.queue_arn
  output_bucket_name       = module.s3.processed_bucket_id
  output_bucket_arn        = module.s3.processed_bucket_arn
  aurora_cluster_arn       = module.aurora.cluster_arn
  aurora_secret_arn        = module.aurora.secret_arn
  aurora_database_name     = module.aurora.database_name
  aurora_security_group_id = module.aurora.security_group_id

  private_subnet_ids = module.vpc.private_subnets

  dlq_arn = module.sqs.dlq_arn
}

module "api_gateway" {
  source = "./../../modules/api_gateway"

  project_name         = var.project_name
  env                  = var.env
  lambda_invoke_arn    = module.lambda.invoke_arn
  lambda_function_name = module.lambda.function_name
  kms_key_arn          = module.security.kms_key_arn
  kms_key_id           = module.security.kms_key_id
  waf_acl_arn          = module.security.waf_acl_arn
}

module "eventbridge" {
  source = "./../../modules/eventbridge"

  project_name          = var.project_name
  env                   = var.env
  target_arn            = module.sqs.queue_arn
  kms_key_arn           = module.security.kms_key_arn
  kms_key_id            = module.security.kms_key_id
  aws_sqs_queue_dlq_arn = module.sqs.dlq_arn
}

module "sqs" {
  source = "./../../modules/sqs"

  project_name = var.project_name
  env = var.env
  
  visibility_timeout = 180  # Match your Lambda timeout
  max_receive_count = 3
  enable_dlq = true

    source_arns = concat(
    module.eventbridge.rule_arns,  # From your EventBridge module
    module.lambda_processors.function_arns  # From your Lambda module
  )
  
  lambda_role_arns = module.lambda_processors.lambda_role_arns
  
  # Optionally override other defaults
  retention_period = 172800      # 2 days
  dlq_retention_period = 604800  # 7 days
  
  tags = var.tags
}

module "security" {
  source = "./../../modules/security"

  project_name = var.project_name
  env          = var.env
  kms_key_arn  = module.security.kms_key_arn
  kms_key_id   = module.security.kms_key_id
}

module "eks_functions" {
  source    = "./modules/eks_functions"
  models    = ["model1", "model2", "model3"]
  prefix    = "myapp"
  namespace = "default"
  replicas  = 3
  sqs_urls = [
    "https://sqs.us-east-2.amazonaws.com/123456789012/EKSModel1Queue",
    "https://sqs.us-east-2.amazonaws.com/123456789012/EKSModel2Queue",
    "https://sqs.us-east-2.amazonaws.com/123456789012/EKSModel3Queue"
  ]
  sqs_arns = [
    "arn:aws:sqs:us-east-2:123456789012:EKSModel1Queue",
    "arn:aws:sqs:us-east-2:123456789012:EKSModel2Queue",
    "arn:aws:sqs:us-east-2:123456789012:EKSModel3Queue"
  ]
  aws_region           = "us-west-2"
  image_tags           = ["latest", "latest", "latest"]
  service_account_name = "eks-service-account"
}

module "aurora" {
  source = "./../../modules/aurora"

  project_name = var.project_name
  env          = var.env
  vpc_id       = module.vpc.vpc_id
  subnet_ids   = module.vpc.private_subnets

  database_name            = "imaige"
  lambda_security_group_id = module.lambda_processors.security_group_id

  min_capacity = 0.5
  max_capacity = 16

  tags = var.tags
}
