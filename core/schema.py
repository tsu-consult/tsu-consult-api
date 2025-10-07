from drf_yasg.inspectors import SwaggerAutoSchema
from rest_framework.permissions import IsAuthenticated

class CustomAutoSchema(SwaggerAutoSchema):
    def get_security(self):
        permissions = self.view.get_permissions()
        if any(isinstance(permission, IsAuthenticated) for permission in permissions):
            return [{'Bearer': []}]
        return []