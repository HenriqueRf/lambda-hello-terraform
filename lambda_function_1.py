def lambda_handler(event, context):
    print("Lambda 1 executada com sucesso!")
    return {
        "statusCode": 200,
        "body": "Lambda 1 executada!"
    }
