# CloudFormation StackSet para deploy de roles nas linked accounts

resource "aws_cloudformation_stack_set" "onevision_data_collector_role" {
  name             = "${local.resource_prefix}-data-collector-stackset"
  description      = "Deploy de roles Lambda DataCollector nas linked accounts"
  permission_model = "SERVICE_MANAGED"

  capabilities = ["CAPABILITY_NAMED_IAM"]

  parameters = {
    ManagementAccountId = var.management_account_id
  }

  template_body = file("${path.module}/cf_template/OneVisionDataCollectorRole.yaml")

  auto_deployment {
    enabled                          = true
    retain_stacks_on_account_removal = false
  }

  managed_execution {
    active = true
  }

  tags = {
    Environment = var.environment
    Project     = "OneVision"
    Client      = var.client_name
  }
}

# StackSet Instance para deploy em todas as contas da organization
resource "aws_cloudformation_stack_set_instance" "onevision_data_collector_role" {
  count = length(var.organizational_unit_ids) > 0 ? 1 : 0

  stack_set_name = aws_cloudformation_stack_set.onevision_data_collector_role.name
  region         = var.region

  deployment_targets {
    organizational_unit_ids = var.organizational_unit_ids
  }
}

# Outputs do StackSet
output "stackset_id" {
  description = "ID do CloudFormation StackSet"
  value       = aws_cloudformation_stack_set.onevision_data_collector_role.id
}

output "stackset_arn" {
  description = "ARN do CloudFormation StackSet"
  value       = aws_cloudformation_stack_set.onevision_data_collector_role.arn
}