from django.db import models


class PaintColor(models.Model):
    year = models.IntegerField(null=True, blank=True)
    manufacturer = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    color_name = models.TextField()
    color_codes = models.TextField()
    color_hex = models.CharField(max_length=10, blank=True, default='')
    normalized_manufacturer = models.CharField(max_length=100, db_index=True)
    normalized_model = models.CharField(max_length=100, db_index=True)
    color_group = models.CharField(max_length=20, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=['normalized_manufacturer', 'normalized_model', 'year', 'color_group']),
        ]

    def __str__(self):
        return f"{self.year} {self.manufacturer} {self.model} - {self.color_name[:40]}"
