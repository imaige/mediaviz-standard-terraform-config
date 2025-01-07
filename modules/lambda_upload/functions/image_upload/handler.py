import base64
import json
import uuid
import time
import os
import boto3
from typing import Dict, Any, Optional, List
from botocore.exceptions import ClientError
from botocore.config import Config
from datetime import datetime
import logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configure AWS RDS Data client
config = Config(
    retries = dict(
        max_attempts = 3
    )
)

class ImageUploadHandler:
    def __init__(self):
        self.s3_client = boto3.client('s3')
        self.eventbridge_client = boto3.client('events')
        self.rds_client = boto3.client('rds-data', config=config)
        self.region = self.s3_client.meta.region_name

    def create_response(self, status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
        """Create standardized API response."""
        return {
            'statusCode': status_code,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps(body)
        }
        
    def upload_to_s3(self, file_content: bytes, file_path: str, bucket_name: str) -> bool:
        """Upload file to S3 with metadata."""
        try:
            self.s3_client.put_object(
                Bucket=bucket_name,
                Key=file_path,
                Body=file_content,
                ContentType='image/jpeg'
            )
            return True
        except ClientError as e:
            logger.error(f"Error uploading to S3: {str(e)}")
            return False
            
    def insert_photo_to_database(
            self,
            request_id: uuid,
            user_id: int,
            company_id: int,
            photo_s3_link: str,
            project_table_name: str,
            client_side_id: str,
            file_path: str,
            title: str,
            description: str,
            format: str,
            size: int,
            source_resolution_x: int,
            source_resolution_y: int,
            date_taken: str,
            latitude: float,
            longitude: float,
    ) -> int:
        date_taken_converted = convert_to_postgres_date(date_taken) if date_taken is not None else None
        response = self.rds_client.execute_statement(
            resourceArn=os.environ['DB_CLUSTER_ARN'],
            secretArn=os.environ['DB_SECRET_ARN'],
            database=os.environ['DB_NAME'],
            sql='''
                INSERT INTO 
                    photos
                VALUES 
                    user_id, 
                    company_id, 
                    photo_s3_link,
                    project_table_name,
                    client_side_id,
                    file_path,
                    title, 
                    description,
                    format,
                    size,
                    source_resolution_x, 
                    source_resolution_y, 
                    date_taken, 
                    date_uploaded,
                    latitude, 
                    longitude 
                RETURNING
                    id
                ''',
            parameters=[
                {'name': 'user_id', 'value': {'longValue': user_id}},
                {'name': 'company_id', 'value': {'longValue': company_id}},
                {'name': 'photo_s3_link', 'value': {'stringValue': photo_s3_link}},
                {'name': 'project_table_name', 'value': {'stringValue': project_table_name}},
                {'name': 'client_side_id', 'value': {'stringValue': client_side_id if client_side_id else None}},
                {'name': 'file_path', 'value': {'stringValue': file_path if file_path else None}},
                {'name': 'title', 'value': {'stringValue': title if title else None}},
                {'name': 'description', 'value': {'stringValue': description if description else None}},
                {'name': 'format', 'value': {'stringValue': format if format else None}},
                {'name': 'size', 'value': {'longValue': size if size else None}},
                {'name': 'source_resolution_x', 'value': {'longValue': source_resolution_x if source_resolution_x else None}},
                {'name': 'source_resolution_y', 'value': {'longValue': source_resolution_y if source_resolution_y else None}},
                {'name': 'date_taken', 'value': {'stringValue': date_taken_converted}},  # handling of None done above
                {'name': 'date_uploaded', 'value': {'stringValue': datetime.now()}},
                {'name': 'latitude', 'value': {'doubleValue': latitude if latitude else None}},
                {'name': 'longitude', 'value': {'doubleValue': longitude if longitude else None}},
            ]
        )
        if response['records']:
            # Add your processing logic here
            generated_photo_id = response['records'][0][0]['longValue']  # Assuming photo_id is an integer
            logger.info(f"Successfully processed photo {generated_photo_id} for client {company_id}")
            return generated_photo_id
        else:
            logger.error(f"Error generating record for photo from request {request_id} for client {company_id}")


    def send_events_to_eventbridge(self, request_id: uuid, bucket: str, key: str, company_id: int, photo_id: int, photo_s3_url: str, models: List[str], timestamp) -> bool:
        """Send processing events to EventBridge."""
        try:
            # Prepare common event details
            common_detail = {
                'request_id': uuid,
                'bucket': bucket,
                'key': key,
                'company_id': company_id,
                'photo_id': photo_id,
                'timestamp': timestamp,
                'version': '1.0'
            }

            # TODO: main upload event should already have happened at this point
            # TODO: need photo S3 link in here so we can access in AI models
            # Send main upload event
            main_event = {
                'Source': 'custom.imageUpload',
                'DetailType': 'ImageUploaded',
                'Detail': json.dumps({
                    **common_detail,
                    'processingType': 'upload'
                }),
                'EventBusName': 'default'
            }

            # Send events for each processing model
            events = [main_event]
            # TODO: iterate over list of models passed rather than arbitrary list
            for model_type in ['lambda', 'eks']:
                for model_num in range(1, 4):
                    model_name = f"{model_type}-model{model_num}"
                    event = {
                        'Source': 'custom.imageUpload',
                        'DetailType': f"{model_name.title()}Processing",
                        'Detail': json.dumps({
                            **common_detail,
                            'processingType': model_name
                        }),
                        'EventBusName': 'default'
                    }
                    events.append(event)

            # Send all events in a single batch
            self.eventbridge_client.put_events(Entries=events)
            return True
            
        except Exception as e:
            logger.error(f"Error sending events to EventBridge: {str(e)}")
            return False

    def handle_upload(self, event: Dict[str, Any], context: Any) -> Dict[str, Any]:
        """Main handler for image upload."""
        try:
            # Validate request body
            if 'body' not in event:
                return self.create_response(400, {'error': 'No file content found'})

            body = event['body']

            # Extract required fields from body
            # TODO: crystallize request format for request/EventBridge event/S3 photo publish that the frontend will use
            models = body.get("models")
            bucket_name = body.get("bucket_name")
            user_id = body.get("user_id")
            company_id = body.get("company_id")
            # TODO: figure out order of ops for generating photo_id in DB and S3 link on AWS
            photo_s3_link = body.get("photo_s3_link")
            project_table_name = body.get("project_table_name")
            client_side_id = body.get("client_side_id")
            file_path = body.get("file_path")
            title = body.get("title")
            description = body.get("description")
            format = body.get("format")
            size = body.get("size")
            source_resolution_x = body.get("source_resolution_x")
            source_resolution_y = body.get("source_resolution_y")
            date_taken = body.get("date_taken")
            latitude = body.get("latitude")
            longitude = body.get("longitude")

            # TODO: validate company_id and user_id?

            # Decode file content
            try:
                file_content = base64.b64decode(body.get("image_data_encoded"))
            except Exception:
                return self.create_response(400, {'error': 'Invalid file content'})

            # Generate file metadata
            timestamp = str(int(time.time()))
            request_id = str(uuid.uuid4())

            # insert into database returning ID for push to future events
            photo_id = self.insert_photo_to_database(
                request_id,
                user_id,
                company_id,
                photo_s3_link,
                project_table_name,
                client_side_id,
                file_path,
                title,
                description,
                format,
                size,
                source_resolution_x,
                source_resolution_y,
                date_taken,
                latitude,
                longitude
            )

            # Generate file path
            file_path = f"uploads/{photo_id}"

            # Upload to S3 - TODO potentially put this before DB insert because S3 link doesn't need to rely on photo_id
            # TODO: add retry logic (potentially on FE as well)
            if not self.upload_to_s3(file_content, file_path, bucket_name):
                return self.create_response(500, {'error': f'Failed to upload file for photo {photo_id}'})

            s3_url = f"https://{bucket_name}.s3.{self.region}.amazonaws.com/{file_path}"

            # Send events to EventBridge - retry handled by DLQ
            if not self.send_events_to_eventbridge(
                bucket_name, request_id, file_path, company_id, photo_id, s3_url, models, timestamp
            ):
                logger.error(f"Warning: EventBridge event sending failed for photo {photo_id}")

            # Return success response
            return self.create_response(200, {
                'message': 'Upload successful',
                'photo_id': photo_id,
                'timestamp': timestamp,
                'company_id': company_id
            })

        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            return self.create_response(500, {'error': 'Internal server error'})

# Initialize handler
handler = ImageUploadHandler()

# Lambda entry point
def handle_upload(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    return handler.handle_upload(event, context)


# Helpers
def convert_to_postgres_date(date_str):
    input_formats = ["%Y-%m-%d", "%m/%d/%y", "%m-%d-%y", "%m/%d/%y %H:%M:%S.%f"]
    for fmt in input_formats:
        try:
            parsed_date = datetime.strptime(date_str, fmt)
            postgres_date = parsed_date.strftime('%Y-%m-%d')
            return postgres_date
        except ValueError as e:
            logger.error(f"Error converting date: {e}")

    raise ValueError("Unknown date format")
