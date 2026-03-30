# spectrum_contacts.py
import json
import os
import random
import math

CONTACTS_FILE = "contacts.json"

class Contact:
    def __init__(self, name, contact_id, shape_type=None):
        self.name = name
        self.id = contact_id
        self.shape_type = shape_type or random.choice(['circle', 'square', 'triangle', 'diamond', 'star'])

class ContactsManager:
    def __init__(self):
        self.contacts = []
        self.load()

    def load(self):
        try:
            if os.path.exists(CONTACTS_FILE):
                with open(CONTACTS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.contacts = [Contact(c['name'], c['id'], c.get('shape_type')) for c in data]
        except Exception as e:
            print(f"Ошибка загрузки контактов: {e}")

    def save(self):
        try:
            data = [{'name': c.name, 'id': c.id, 'shape_type': c.shape_type} for c in self.contacts]
            with open(CONTACTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Ошибка сохранения контактов: {e}")

    def get_available_shape_type(self):
        """Возвращает первую свободную форму из 30 возможных"""
        used_shapes = {c.shape_type for c in self.contacts}
        all_shapes = [f"shape_{i}" for i in range(30)]   # shape_0 ... shape_29
        for shape in all_shapes:
            if shape not in used_shapes:
                return shape
        return random.choice(all_shapes)

    def add_contact(self, name, contact_id):
        # проверка на существование ID
        if any(c.id == contact_id for c in self.contacts):
            return None
        shape_type = self.get_available_shape_type()
        contact = Contact(name, contact_id, shape_type)
        self.contacts.append(contact)
        self.save()
        return contact

    def remove_contact(self, contact_id):
        for i, c in enumerate(self.contacts):
            if c.id == contact_id:
                del self.contacts[i]
                self.save()
                return True
        return False

    def get_contact_by_id(self, contact_id):
        for c in self.contacts:
            if c.id == contact_id:
                return c
        return None

    def get_contact_by_name(self, name):
        for c in self.contacts:
            if c.name.lower() == name.lower():
                return c
        return None

    def list_contacts(self):
        return self.contacts