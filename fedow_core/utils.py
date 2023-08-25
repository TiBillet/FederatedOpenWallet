import base64
import json

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature
import logging

logger = logging.getLogger(__name__)


def b64encode(string):
    return base64.urlsafe_b64encode(string.encode('utf-8')).decode('utf-8')

def b64decode(string):
    return base64.urlsafe_b64decode(string).decode('utf-8')

def jsonb64decode(string):
    return json.loads(base64.urlsafe_b64decode(string).decode('utf-8'))

def jsonb64encode(dico: dict):
    return base64.urlsafe_b64encode(json.dumps(dico).encode('utf-8')).decode('utf-8')


def gen_fernet_key():
    return Fernet.generate_key().decode('utf-8')

def get_request_ip(request):
    # logger.info(request.META)
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    x_real_ip = request.META.get('HTTP_X_REAL_IP')

    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    elif x_real_ip:
        ip = x_real_ip
    else:
        ip = request.META.get('REMOTE_ADDR')

    return ip


def sign_message(message: str, private_key):
    # private_key = get_private_key()
    b_message = message.encode('utf-8')
    signature = private_key.sign(
        b_message,
        padding=padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH
        ),
        algorithm=hashes.SHA256()
    )
    return base64.urlsafe_b64encode(signature)

def verify_signature(public_key: str,
                   message: str,
                   signature: str):
    # Vérifier la signature
    try:
        public_key = serialization.load_pem_public_key(public_key.encode('utf'))
        public_key.verify(
            base64.urlsafe_b64decode(signature),
            message.encode('utf-8'),
            padding=padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            algorithm=hashes.SHA256()
        )
        return True
    except InvalidSignature:
        return False
