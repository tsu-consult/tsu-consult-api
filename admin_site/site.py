from django.contrib.admin import AdminSite

class TSUAdminSite(AdminSite):
    site_header = "TSU Consult Admin"
    site_title = "TSU Consult"
    index_title = "Welcome to the TSU Consult Admin"

admin_site = TSUAdminSite(name='tsu_admin')
