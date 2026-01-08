from django.db import models

class Bucket(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_public = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class Object(models.Model):
    bucket = models.ForeignKey(Bucket, on_delete=models.CASCADE)
    key = models.CharField(max_length=512)
    size = models.BigIntegerField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
