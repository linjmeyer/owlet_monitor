#!/usr/bin/python3

import sys, os, time, requests, json
from .owlet_status import OwletStatus
from .exporters import PrometheusExporter

sess = None
url_props = None
url_activate = None
headers = {}
auth_token = None
expire_time = 0
dsn = None
owlet_region = 'world'
region_config = {
    'world': {
        'url_mini': 'https://ayla-sso.owletdata.com/mini/',
        'url_signin': 'https://user-field-1a2039d9.aylanetworks.com/api/v1/token_sign_in',
        'url_base': 'https://ads-field-1a2039d9.aylanetworks.com/apiv1',
        'apiKey': 'AIzaSyCsDZ8kWxQuLJAMVnmEhEkayH1TSxKXfGA',
        'app_id': 'sso-prod-3g-id',
        'app_secret': 'sso-prod-UEjtnPCtFfjdwIwxqnC0OipxRFU',
    },
    'europe': {
        'url_mini': 'https://ayla-sso.eu.owletdata.com/mini/',
        'url_signin': 'https://user-field-eu-1a2039d9.aylanetworks.com/api/v1/token_sign_in',
        'url_base': 'https://ads-field-eu-1a2039d9.aylanetworks.com/apiv1',
        'apiKey': 'AIzaSyDm6EhV70wudwN3iOSq3vTjtsdGjdFLuuM',
        'app_id': 'OwletCare-Android-EU-fw-id',
        'app_secret': 'OwletCare-Android-EU-JKupMPBoj_Npce_9a95Pc8Qo0Mw',
    }
}


class FatalError(Exception):
    pass


def log(s):
    sys.stderr.write(s + '\n')
    sys.stderr.flush()


def record(s):
    sys.stdout.write(s + '\n')
    sys.stdout.flush()


def login():
    global auth_token, expire_time, owlet_region
    try:
        owlet_user, owlet_pass = os.environ['OWLET_USER'], os.environ['OWLET_PASS']
        if not len(owlet_user):
            raise FatalError("OWLET_USER is empty")
        if not len(owlet_pass):
            raise FatalError("OWLET_PASS is empty")
    except KeyError as e:
        raise FatalError("OWLET_USER or OWLET_PASS env var is not defined")
    if 'OWLET_REGION' in os.environ:
        owlet_region = os.environ['OWLET_REGION']
    if owlet_region not in region_config:
        raise FatalError("OWLET_REGION env var '{}' not recognised - must be one of {}".format(
            owlet_region, region_config.keys()))
    if auth_token is not None and (expire_time > time.time()):
        return
    log('Logging in')
    # authenticate against Firebase, get the JWT.
    # need to pass the X-Android-Package and X-Android-Cert headers because
    # the API key is restricted to the Owlet Android app
    # https://cloud.google.com/docs/authentication/api-keys#api_key_restrictions
    api_key = region_config[owlet_region]['apiKey']
    r = requests.post(f'https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword?key={api_key}',
            data=json.dumps({'email': owlet_user, 'password': owlet_pass, 'returnSecureToken': True}),
            headers={
                'X-Android-Package': 'com.owletcare.owletcare',
                'X-Android-Cert': '2A3BC26DB0B8B0792DBE28E6FFDC2598F9B12B74'
        })
    r.raise_for_status()
    jwt = r.json()['idToken']
    # authenticate against owletdata.com, get the mini_token
    r = requests.get(region_config[owlet_region]
                     ['url_mini'], headers={'Authorization': jwt})
    r.raise_for_status()
    mini_token = r.json()['mini_token']
    # authenticate against Ayla, get the access_token
    r = requests.post(region_config[owlet_region]['url_signin'], json={
                "app_id": region_config[owlet_region]['app_id'],
                "app_secret": region_config[owlet_region]['app_secret'],
                "provider": "owl_id",
                "token": mini_token,
                })
    r.raise_for_status()
    auth_token = r.json()['access_token']
    # we will re-auth 60 seconds before the token expires
    expire_time = time.time() + r.json()['expires_in'] - 60
    headers['Authorization'] = 'auth_token ' + auth_token
    log('Auth token %s' % auth_token)


def fetch_dsn():
    global dsn, url_props, url_activate
    if dsn is None:
        log('Getting DSN')
        r = sess.get(region_config[owlet_region]
                     ['url_base'] + '/devices.json', headers=headers)
        r.raise_for_status()
        devs = r.json()
        if len(devs) < 1:
            raise FatalError('Found zero Owlet monitors')
        # Allow for multiple devices
        dsn = []
        url_props = []
        url_activate = []
        for device in devs:
            device_sn = device['device']['dsn']
            dsn.append(device_sn)
            log('Found Owlet monitor device serial number %s' % device_sn)
            url_props.append(
                region_config[owlet_region]['url_base'] + '/dsns/' + device_sn
                + '/properties.json'
            )
            url_activate.append(
                region_config[owlet_region]['url_base'] + '/dsns/' + device_sn
                + '/properties/APP_ACTIVE/datapoints.json'
            )


def reactivate(url_activate):
    payload = { "datapoint": { "metadata": {}, "value": 1 } }
    r = sess.post(url_activate, json=payload, headers=headers)
    r.raise_for_status()


def fetch_props():
    # Ayla cloud API data is updated only when APP_ACTIVE periodically reset to 1.
    my_props = []
    # Get properties for each device; note no pause between requests for each device
    for device_sn,next_url_activate,next_url_props in zip(dsn, url_activate, url_props):
        reactivate(next_url_activate)
        device_props = {'DSN': device_sn}
        r = sess.get(next_url_props, headers=headers)
        r.raise_for_status()
        props = r.json()
        for prop in props:
            n = prop['property']['name']
            del(prop['property']['name'])
            device_props[n] = prop['property']
        my_props.append(device_props)
    return my_props


def record_vitals(p) -> OwletStatus:
    status = OwletStatus(p)
    record(json.dumps(status.__dict__))
    return status


def loop():
    exporters = [PrometheusExporter()]
    global sess
    sess = requests.session()
    while True:
        try:
            login()
            fetch_dsn()
            for prop in fetch_props():
                status = record_vitals(prop)
                if status:
                    for exporter in exporters:
                        exporter.export(status)
            time.sleep(10)
        except requests.exceptions.RequestException as e:
            log('Network error: %s' % e)
            time.sleep(1)
            sess = requests.session()

