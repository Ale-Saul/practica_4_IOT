import os
import json
import logging
import boto3
import time
from botocore.exceptions import ClientError
from decimal import Decimal  # Import Decimal to handle floating-point numbers
from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import AbstractRequestHandler, AbstractExceptionHandler
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_core.utils import get_request_type, get_intent_name
from ask_sdk_model import Response

# Logging configuration
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Create the SSM client before using it
ssm_client = boto3.client('ssm')

# Function to get parameters from the Parameter Store
def get_parameter(name):
    try:
        response = ssm_client.get_parameter(Name=name, WithDecryption=True)
        return response['Parameter']['Value']
    except ClientError as e:
        logger.error(f"Error getting parameter {name}: {e}")
        raise Exception(f"Error getting parameter {name}: {e}")

# Get the thing name, endpoint, and database from Parameter Store
thing_name = get_parameter('/sistema_riego/thing_name')
endpoint_url = get_parameter('/sistema_riego/endpoint_url')
db = get_parameter('/sistema_riego/database')

# Create IoT client with dynamic endpoint
iot_client = boto3.client('iot-data', endpoint_url=endpoint_url)

# Configure DynamoDB client
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(db)

# Water flow rate in liters per minute
FLOW_RATE = 10

# Function to insert data into DynamoDB
def insert_irrigation_data(event_id, start_time, end_time, duration, water_volume, soil_moisture, thing_id, system_status, request_time):
    try:
        # Convert float values to Decimal
        item = {
            'ID': event_id,
            'StartTime': start_time,
            'EndTime': end_time,
            'Duration': Decimal(str(duration)) if duration is not None else None,  # Convert to Decimal
            'WaterVolume': Decimal(str(water_volume)) if water_volume is not None else None,  # Convert to Decimal
            'SoilMoisture': soil_moisture if soil_moisture is not None else None,
            'ThingID': thing_id,
            'SystemStatus': system_status,
            'RequestTime': request_time
        }
        table.put_item(Item=item)
        logger.info(f"Data inserted into DynamoDB: {item}")
    except Exception as e:
        logger.error(f"Error inserting data into DynamoDB: {e}")

# Global variables to register irrigation start time
start_time_global = None

# Launch request handler
class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return get_request_type(handler_input) == "LaunchRequest"

    def handle(self, handler_input):
        speak_output = 'Bienvenido al sistema de riego. Puedes decir "activar el regador", "desactivar el regador", o "consultar humedad".'
        return handler_input.response_builder.speak(speak_output).reprompt(speak_output).response

# Handler to activate the sprinkler
class ActivateSprinklerIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return get_intent_name(handler_input) == "ActivarRegadorIntent"

    def handle(self, handler_input):
        global start_time_global
        payload = {"state": {"desired": {"pump": "ON"}}}
        start_time_global = time.time()  # Register start time in seconds
        try:
            iot_client.update_thing_shadow(thingName=thing_name, payload=json.dumps(payload))
            speak_output = 'El regador ha sido activado.'
        except Exception as e:
            logger.error(f"Error activating the sprinkler: {e}")
            speak_output = 'Hubo un problema al activar el regador, por favor intenta nuevamente.'
        return handler_input.response_builder.speak(speak_output).response

# Handler to deactivate the sprinkler
class DeactivateSprinklerIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return get_intent_name(handler_input) == "DesactivarRegadorIntent"

    def handle(self, handler_input):
        global start_time_global
        payload = {"state": {"desired": {"pump": "OFF"}}}
        end_time = time.time()  # Current time in seconds
        try:
            iot_client.update_thing_shadow(thingName=thing_name, payload=json.dumps(payload))
            if start_time_global:
                duration = (end_time - start_time_global) / 60  # Duration in minutes
                water_volume = duration * FLOW_RATE  # Water volume
                event_id = f"{int(end_time)}-{thing_name}"  # Unique ID based on time and device
                time.sleep(1)
                response = iot_client.get_thing_shadow(thingName=thing_name)
                json_state = json.loads(response["payload"].read())
                humidity = json_state["state"]["reported"].get("humedad", None)

                # Insert data into DynamoDB
                insert_irrigation_data(
                    event_id=event_id,
                    start_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_time_global)),
                    end_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end_time)),
                    duration=round(duration, 2),
                    water_volume=round(water_volume, 2),
                    soil_moisture=humidity,  # Only store humidity explicitly when queried
                    thing_id=thing_name,
                    system_status="Desactivado",
                    request_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end_time))
                )
                speak_output = f'El regador ha sido desactivado. Se utilizaron aproximadamente {round(water_volume, 2)} litros de agua.'
                start_time_global = None  # Reset start time
            else:
                speak_output = 'El regador no estaba activado.'
        except Exception as e:
            logger.error(f"Error deactivating the sprinkler: {e}")
            speak_output = 'Hubo un problema al desactivar el regador, por favor intenta nuevamente.'
        return handler_input.response_builder.speak(speak_output).response

# Handler to query the humidity
class QueryHumidityIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return get_intent_name(handler_input) == "ConsultarHumedadIntent"

    def handle(self, handler_input):
        global start_time_global
        try:
            iot_client.publish(
                topic="sistema_riego/solicitud_humedad",
                qos=1,
                payload=json.dumps({"message": "SOLICITAR_HUMEDAD"})
            )
            time.sleep(1)
            response = iot_client.get_thing_shadow(thingName=thing_name)
            json_state = json.loads(response["payload"].read())
            humidity = json_state["state"]["reported"].get("humedad", None)
            
            if humidity is None:
                speak_output = "No puedo obtener la humedad en este momento."
            else:
                state = "húmedo" if humidity < 1300 else "seco"
                speak_output = f"El nivel de humedad es {humidity}. El suelo está {state}."
                # Insert data into DynamoDB
                event_id = f"{time.time()}-{thing_name}"
                request_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))  # Request time
                
                if humidity > 1300:  # If humidity is greater, store start and humidity
                    event_id = f"{time.time()}-{thing_name}"
                    start_time_global = time.time()  # Register request time
                    insert_irrigation_data(
                        event_id=event_id,
                        start_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_time_global)),
                        end_time=None,
                        duration=None,
                        water_volume=None,
                        soil_moisture=humidity,
                        thing_id=thing_name,
                        system_status="Consulta Humedad",
                        request_time=request_time  # Store request time
                    )
                else:  # If humidity is lower
                    if start_time_global:
                        end_time = time.time()  # Current time when querying humidity
                        duration = (end_time - start_time_global) / 60  # Duration in minutes
                        water_volume = duration * FLOW_RATE  # Water volume in liters
                        # Insert data with duration and water volume
                        insert_irrigation_data(
                            event_id=event_id,
                            start_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_time_global)),
                            end_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end_time)),
                            duration=round(duration, 2),
                            water_volume=round(water_volume, 2),
                            soil_moisture=humidity,
                            thing_id=thing_name,
                            system_status="Consulta Humedad",
                            request_time=request_time  # Store request time
                        )
                    else:
                        # If no start_time_global, only store start and humidity
                        start_time_global = time.time()
                        insert_irrigation_data(
                            event_id=event_id,
                            start_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_time_global)),
                            end_time=None,
                            duration=None,
                            water_volume=None,
                            soil_moisture=humidity,
                            thing_id=thing_name,
                            system_status="Consulta Humedad",
                            request_time=request_time  # Store request time
                        )
        except Exception as e:
            logger.error(f"Error getting humidity: {e}")
            speak_output = "Hubo un problema al obtener la humedad. Por favor, intenta nuevamente."

        return handler_input.response_builder.speak(speak_output).response

# Error handler
class ErrorHandler(AbstractExceptionHandler):
    def can_handle(self, handler_input, exception):
        return True

    def handle(self, handler_input, exception):
        logger.error(f"Error handled: {exception}")
        speak_output = 'Lo siento, hubo un problema. Por favor intenta nuevamente.'
        return handler_input.response_builder.speak(speak_output).response

# Skill builder construction
sb = SkillBuilder()
sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(ActivateSprinklerIntentHandler())
sb.add_request_handler(DeactivateSprinklerIntentHandler())
sb.add_request_handler(QueryHumidityIntentHandler())
sb.add_exception_handler(ErrorHandler())

lambda_handler = sb.lambda_handler()
