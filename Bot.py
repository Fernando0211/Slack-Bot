import os
import json
import asyncio
import requests
from jira import JIRA
from typing import List, Dict
from dotenv import load_dotenv
from slack_sdk import WebClient
from collections import OrderedDict
from jira.exceptions import JIRAError
from flask import Flask, request, Response
from slackeventsapi import SlackEventAdapter

# Configuración: Carga y almacena tokens y variables de entorno
class Config:
    def __init__(self):
        load_dotenv()
        self.slack_signing_secret = os.environ['SLACK_SIGNING_SECRET']
        self.slack_token = os.getenv('SLACK_TOKEN')
        self.slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL")
        self.slack_client = WebClient(token=self.slack_token)

        self.dify_url = os.getenv("DIFY_URL")
        self.dify_api_key = f"Bearer {os.getenv('API_KEY')}"
        self.user_id = os.getenv('USER_ID')
        
        self.jira_server = "https://" + os.getenv("JIRA_SERVER_URL")
        self.jira_user = os.getenv("JIRA_EMAIL")
        self.jira_api_token = os.getenv("JIRA_API_TOKEN")

# Cache: Evita el procesamiento duplicado de eventos
# Usado en BotAI.handle_app_mention y BotAI.handle_direct_message
class EventCache:
    def __init__(self, max_size=1000):
        self.cache = OrderedDict()
        self.max_size = max_size

    def add(self, event_id):
        if event_id not in self.cache:
            self.cache[event_id] = True
            if len(self.cache) > self.max_size:
                self.cache.popitem(last=False)

    def exists(self, event_id):
        return event_id in self.cache

# Cliente Dify: Maneja la comunicación con el servicio de Dify.AI
# Usado en BotAI.handle_app_mention y BotAI.handle_direct_message
class DifyClient:
    def __init__(self, config):
        self.url = config.dify_url
        self.headers = {
            "Authorization": config.dify_api_key,
            "Content-Type": "application/json"
        }
        self.user_id = config.user_id
    #Metodo para enviar un mensaje a Dify
    def send_message(self, query, conversations):
        payload = {
            "inputs": {},
            "query": query,
            "response_mode": "blocking",
            "conversation_id": conversations.get("channel_id", ""),
            "user": self.user_id
        }
        response = requests.post(self.url, headers=self.headers, data=json.dumps(payload))
        return response

# Gestor de Slack: Maneja la comunicación con Slack
# Usado en varios métodos de BotAI para enviar mensajes
class SlackManager:
    def __init__(self, config):
        self.client = WebClient(token=config.slack_token)
        self.bot_id = self.client.api_call('auth.test')['user_id']

    #Metodo para enviar un mensaje a un canal de Slack
    def send_message(self, channel, text):
        self.client.chat_postMessage(channel=channel, text=text)

# Gestor de Jira: Maneja la conexión y consultas a Jira
# Usado en BotAI.jira_backlog_slack
class JiraManager:
    def __init__(self, config):
        self.config = config
        self.jira_client = None
    
    #Metodo para conectar a Jira
    def connect(self):
        options = {'server': self.config.jira_server}
        try:
            self.jira_client = JIRA(options, basic_auth=(self.config.jira_user, self.config.jira_api_token))
            user_info = self.jira_client.myself()
            print(f"Connected to Jira as: {user_info['displayName']}")
        except JIRAError as e:
            print(f"Jira connection error: {str(e)}")
            if e.status_code:
                print(f"HTTP status code: {e.status_code}")
            print(f"Error message: {e.text}")
            exit()
    
    #Metodo para obtener las tareas del backlog de Jira
    def get_backlog_issues(self, jql_query: str, max_results: int) -> List[Dict]:
        if not self.jira_client:
            self.connect()
        return self.jira_client.search_issues(jql_query, maxResults=max_results)

# Generador de mensajes de Slack: Crea mensajes formateados para Slack
# Usado en BotAI.jira_backlog_slack
class SlackMessageGenerator:
    def __init__(self, webhook_url):
        self.webhook_url = webhook_url
        self.status_colors = {
            "0 Backlog": "#8FBC8F",  # Verde oscuro
            "0 FUNNEL": "#00CED1",   # Turquesa
            "1 COMMITTED": "#FFD700", # Amarillo
            "2 READY": "#FFA500",     # Naranja
            "3 IN PROGRESS": "#007bff", # Azul
            "4 VALIDATE": "#6A5ACD",  # Azul oscuro
            "Finalizada": "#d9534f"   # Rojo
        }

    #Metodo para generar todo el attachment para Slack
    def generate_message(self, issues_in_backlog):
        tasks_by_status = self._group_tasks_by_status(issues_in_backlog)
        attachments = self._create_attachments(tasks_by_status)
        return {"attachments": attachments}

    #Metodo para agrupar las tareas por estado
    def _group_tasks_by_status(self, issues):
        tasks_by_status = {status: [] for status in self.status_colors.keys()}
        
        for issue in issues:
            status_name = issue.fields.status.name
            field = {
                "title": f"*{issue.key}*",
                "value": f"Resumen: _{issue.fields.summary}_\nEstado: *{status_name}*",
                "short": False
            }
            
            if status_name in tasks_by_status:
                tasks_by_status[status_name].append(field)
        
        return tasks_by_status

    #Metodo para crear los attachments para Slack
    def _create_attachments(self, tasks_by_status):
        attachments = []
        for status, fields in tasks_by_status.items():
            if fields:
                attachments.append({
                    "color": self.status_colors[status],
                    "title": f"*{status}*",
                    "text": "*Tareas:*",
                    "fields": fields,
                    "footer": "Jira",
                })
        return attachments

    #Metodo para enviar el attachment a Slack
    def send_message(self, payload):
        response = requests.post(self.webhook_url, json=payload)
        if response.status_code == 200:
            print("Mensaje destacado enviado a Slack correctamente.")
        else:
            print(f"Error al enviar el mensaje a Slack. Código de estado: {response.status_code}")
        return response

# Clase principal BotAI: Integra todas las funcionalidades
class BotAI:
    def __init__(self):
        # Inicialización de componentes
        self.config = Config()
        self.app = Flask(__name__)
        self.event_cache = EventCache()
        self.dify_client = DifyClient(self.config)
        self.jira_manager = JiraManager(self.config)
        self.slack_event_adapter = SlackEventAdapter(
            self.config.slack_signing_secret, '/slack/events', self.app)
        self.slack_bot = SlackManager(self.config)
        self.slack_message_generator = SlackMessageGenerator(self.config.slack_webhook_url)
        self.conversations = {}

    # Maneja menciones de la app en Slack
    def handle_app_mention(self, payload):
        # Extraer información del evento
        event = payload.get('event', {})
        event_id = event.get('event_ts')

        # Verificar si el evento ya ha sido procesado
        if self.event_cache.exists(event_id):
            return

        # Agregar el evento al caché para evitar procesamiento duplicado
        self.event_cache.add(event_id)

        # Extraer detalles relevantes del evento
        text = event.get('text')
        channel = event.get('channel')
        user = event.get('user')

        # Verificar que el mensaje no fue enviado por el bot mismo
        if self.slack_bot.bot_id != user:
            # Obtener el ID de conversación existente para el canal, si existe
            conversation_id = self.conversations.get(channel, '')
            
            # Enviar el mensaje a Dify para procesamiento
            response = self.dify_client.send_message(text, {"channel_id": conversation_id})

            if response.status_code == 200:
                # Procesar la respuesta exitosa de Dify
                response_data = response.json()
                reply_text = response_data.get('answer', 'No response from Dify.')
                
                # Actualizar o almacenar el ID de conversación para futuras interacciones
                self.conversations[channel] = response_data.get('conversation_id', '')
                
                # Enviar la respuesta de Dify al canal de Slack
                self.slack_bot.send_message(channel, f'Response from Dify: {reply_text}')
            else:
                # Manejar errores en la comunicación con Dify
                self.slack_bot.send_message(channel, f'Error contacting Dify: {response.status_code}')

    # Maneja mensajes directos en Slack
    def handle_direct_message(self, payload):
        event = payload.get('event', {})
        event_id = event.get('event_ts')

        if self.event_cache.exists(event_id):
            return

        self.event_cache.add(event_id)
        
        text = event.get('text')
        channel = event.get('channel')
        user = event.get('user')
        channel_type = event.get('channel_type')

        if self.slack_bot.bot_id != user:  # Asegurar de que el mensaje no provenga del bot mismo
        # Responder en mensajes directos
            # Usar el id de la conversacion del canal si existe
            conversation_id = self.conversations.get(channel, '')
            response = self.dify_client.send_message(text, {"channel_id": conversation_id})

            if response.status_code == 200:
                response_data = response.json()
                reply_text = response_data.get('answer', 'No response from Dify.')
            
                if channel_type == 'im':
                    # Almacenar o actualizar el id de la conversacion
                    self.conversations[channel] = response_data.get('conversation_id', '')
                    self.slack_bot.send_message(channel, f'Response from Dify: {reply_text}')
            else:
                self.slack_bot.send_message(channel, f'Error contacting Dify: {response.status_code}')

    # Maneja solicitudes de tareas de Jira
    async def handle_tareas_jira(self, channel_id, text):
        # Divide el texto del comando en partes
        parts = text.split()
        
        # Verifica si el comando tiene el formato correcto
        if len(parts) >= 4 and parts[0].lower() == 'proyecto:' and parts[2].lower() == 'tareas:':
            # Extrae el nombre del proyecto
            project = parts[1]
            try:
                # Intenta convertir el número de tareas a un entero
                num_tasks = int(parts[3])
            except ValueError:
                # Si la conversión falla, envía un mensaje de error
                error_message = "Numero de tareas invalido. Por favor proporciona un numero entero valido."
                self.slack_bot.send_message(channel_id, error_message)
                return Response(error_message, status=400)

            # Construye la consulta JQL para Jira
            jql_query = f'project = {project}'
            
            # Crea una tarea asíncrona para obtener y enviar el backlog de Jira
            asyncio.create_task(self.async_jira_backlog_slack(jql_query, num_tasks))
            
            # Envía un mensaje de confirmación a Slack
            success_message = f"Obteniendo {num_tasks} tareas del proyecto {project}."
            self.slack_bot.send_message(channel_id, success_message)
            return Response(success_message, status=200)
        
        else:
            # Si el formato del comando es incorrecto, envía un mensaje de uso
            usage_message = "Formato Invalido. Por favor usa: /tareas-jira Proyecto: <project_name> Tareas: <number_of_tasks>"
            self.slack_bot.send_message(channel_id, usage_message)
            return Response(usage_message, status=400)

    # Tarea asíncrona para obtener y enviar el backlog de Jira
    async def async_jira_backlog_slack(self, jql_query: str, max_results: int):
        await asyncio.to_thread(self.jira_backlog_slack, jql_query, max_results)

    # Obtiene y envía el backlog de Jira a Slack
    def jira_backlog_slack(self, jql_query: str, max_results: int):
        issues_in_backlog = self.jira_manager.get_backlog_issues(jql_query, max_results)
        message_payload = self.slack_message_generator.generate_message(issues_in_backlog)
        self.slack_message_generator.send_message(message_payload)

    # Inicia la aplicación Flask
    def run(self):
        self.app.run(debug=True, use_reloader=True)

# Inicialización de la instancia BotAI
bot = BotAI()

# Manejador de eventos para menciones de la app
@bot.slack_event_adapter.on('app_mention')
def app_mention(payload):
    bot.handle_app_mention(payload)
    
@bot.slack_event_adapter.on('message')    
def app_message(payload):
    bot.handle_direct_message(payload)

# Ruta para el comando de tareas de Jira
@bot.app.route('/tareas-jira', methods=['POST'])
async def tareas_jira():
    data = request.form
    channel_id = data.get('channel_id')
    text = data.get('text')
    return await bot.handle_tareas_jira(channel_id, text)

# Punto de entrada principal
if __name__ == '__main__':
    bot.run()
