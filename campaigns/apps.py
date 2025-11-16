from django.apps import AppConfig

'''
App File where we can assign app name of our project. This name should be included in settings.py,project url linkingg
'''
class CampaignsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'campaigns'
