import json
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'coloureg.settings')
django.setup()

from lookup.models import PaintColor


def import_database(filepath):
    print("Loading JSON...")
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    print(f"Loaded {len(data)} records")
    print("Clearing existing data...")
    PaintColor.objects.all().delete()

    print("Importing...")
    batch = []
    batch_size = 5000

    for i, record in enumerate(data):
        batch.append(PaintColor(
            year=record.get('year'),
            manufacturer=record.get('manufacturer', ''),
            model=record.get('model', ''),
            color_name=record.get('color_name', ''),
            color_codes=record.get('color_codes', ''),
            color_hex=record.get('color_hex', ''),
            normalized_manufacturer=record.get('normalized_manufacturer', ''),
            normalized_model=record.get('normalized_model', ''),
            color_group=record.get('color_group', ''),
        ))

        if len(batch) >= batch_size:
            PaintColor.objects.bulk_create(batch)
            batch = []
            print(f"  {i + 1} records imported...")

    if batch:
        PaintColor.objects.bulk_create(batch)

    total = PaintColor.objects.count()
    print(f"\nDone! {total} records in database.")


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2:
        print("Usage: python import_data.py merged_database.json")
        sys.exit(1)
    import_database(sys.argv[1])