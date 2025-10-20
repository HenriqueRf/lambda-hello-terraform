import boto3
import os
from datetime import datetime

def lambda_handler(event, context):
    s3 = boto3.client('s3')
    bucket = os.environ['BUCKET_NAME']

    now = datetime.utcnow().strftime('%Y-%m-%d_%H-%M-%S')
    message = f"Lambda executada com sucesso em {now} UTC!"

    # Salva log no S3
    s3.put_object(
        Bucket=bucket,
        Key=f"logs/lambda_run_{now}.txt",
        Body=message.encode('utf-8')
    )

    # Retorno padrão
    return {
        'statusCode': 200,
        'body': message
    }
