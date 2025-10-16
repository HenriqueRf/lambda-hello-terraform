# AWS Lambda Hello World - Terraform Cloud

Este repositório cria uma função **AWS Lambda** simples com Terraform Cloud.

## 🔧 Estrutura
- `main.tf`: Configuração principal (IAM Role, Lambda, empacotamento)
- `lambda_function.py`: Código Python da função
- `variables.tf`: Variáveis de região
- `outputs.tf`: Saída com o nome da Lambda

## 🧭 Passos para usar

1. Crie um **Workspace** no Terraform Cloud (workflow “Version Control”)
2. Conecte este repositório (GitHub)
3. No Terraform Cloud, adicione variáveis de ambiente:

| Tipo | Nome | Valor |
|------|------|--------|
| Environment variable | `AWS_ACCESS_KEY_ID` | sua Access Key |
| Environment variable | `AWS_SECRET_ACCESS_KEY` | sua Secret Key |
| Environment variable | `AWS_DEFAULT_REGION` | `us-east-1` |

4. Clique em **Run → Apply**
5. Após o deploy, verifique a Lambda no console AWS:
   - [https://console.aws.amazon.com/lambda](https://console.aws.amazon.com/lambda)

✅ **Resultado:** Lambda `hello_world_lambda` criada e executando “Hello, Terraform from AWS Lambda!”.
