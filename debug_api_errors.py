#!/usr/bin/env python
import os
import django
import json

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bookclub.settings')
django.setup()

from rest_framework.test import APIClient
from core.models import User
from django.urls import reverse

# Создаём API клиент
client = APIClient()

# Удаляем существующего пользователя если есть
User.objects.filter(email='test_duplicate_123@example.com').delete()

# Создаём пользователя с дублирующимся email
User.objects.create_user(
    username='existing_user_123',
    email='test_duplicate_123@example.com',
    password='StrongPass123',
    first_name='Test',
    last_name='User',
)

# Пытаемся зарегистрировать нового пользователя с тем же email
response = client.post(
    '/api/register/',
    {
        'username': 'new_username_123',
        'email': 'test_duplicate_123@example.com',
        'first_name': 'New',
        'last_name': 'User',
        'password': 'StrongPass123',
        'passwordConfirm': 'StrongPass123',
        'accept_privacy_policy': True,
        'accept_terms': True,
        'accept_personal_data_processing': True,
    },
    format='json',
)

print("Status Code:", response.status_code)
print("\nResponse Data (raw):")
print(response.data)
print("\nResponse Data (JSON):")
print(json.dumps(response.data, indent=2, ensure_ascii=False))
