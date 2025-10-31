def lambda_handler(event, context):
    print("Lambda 2 executada com sucesso!")
    return {
        "statusCode": 200,
        "body": "Lambda 2 executada!"
    }
