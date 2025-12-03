from django.contrib.admin import AdminSite


class TSUAdminSite(AdminSite):
    site_header = "TSU Consult"
    site_title = "TSU Consult"
    index_title = "Welcome to the TSU Consult"


admin_site = TSUAdminSite(name='tsu_admin')
