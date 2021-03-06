from django.http import HttpResponse, Http404
from django.shortcuts import render, redirect
from django.views.decorators.cache import never_cache
from django.urls import reverse
from django.core.files.uploadhandler import TemporaryFileUploadHandler
from brutaldon.forms import LoginForm, OAuthLoginForm, SettingsForm, PostForm
from brutaldon.models import Client, Account
from mastodon import Mastodon
from urllib import parse
from pdb import set_trace

class NotLoggedInException(Exception):
    pass

class Singleton(type):
    _instances = {}
    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]

class MastodonPool(dict, metaclass=Singleton):
    pass

def get_mastodon(request):
    if not (request.session.has_key('instance') and
            (request.session.has_key('username') or
             request.session.has_key('access_token'))):
        raise NotLoggedInException()

    pool = MastodonPool()
    if request.session.has_key('access_token'):
        try:
            client = Client.objects.get(api_base_id=request.session['instance'])
        except (Client.DoesNotExist, Client.MultipleObjectsReturned):
            raise NotLoggedInException()
        if request.session['access_token'] in pool.keys():
            mastodon = pool[request.session['access_token']]
        else:
            mastodon = Mastodon(
                client_id = client.client_id,
                client_secret = client.client_secret,
                api_base_url = client.api_base_id,
                access_token = request.session['access_token'],
                ratelimit_method='throw')
            pool[request.session['access_token']] = mastodon
    else:
        try:
            client = Client.objects.get(api_base_id=request.session['instance'])
            user = Account.objects.get(username=request.session['username'])
        except (Client.DoesNotExist, Client.MultipleObjectsReturned,
                Account.DoesNotExist, Account.MultipleObjectsReturned):
            raise NotLoggedInException()
        if user.access_token in pool.keys():
            mastodon = pool[user.access_token]
        else:
            mastodon = Mastodon(
                client_id = client.client_id,
                client_secret = client.client_secret,
                access_token = user.access_token,
                api_base_url = client.api_base_id,
                ratelimit_method="throw")
    return mastodon

def fullbrutalism_p(request):
    if request.session.has_key('fullbrutalism'):
        fullbrutalism = request.session['fullbrutalism']
    else:
        fullbrutalism = False
    return fullbrutalism

def timeline(request, timeline='home', timeline_name='Home', max_id=None, since_id=None):
    try:
        mastodon = get_mastodon(request)
    except NotLoggedInException:
        return redirect(about)
    data = mastodon.timeline(timeline, limit=100, max_id=max_id, since_id=since_id)
    form = PostForm(initial={'visibility': request.session['user'].source.privacy})
    try:
        prev = data[0]._pagination_prev
        if len(mastodon.timeline(since_id=prev['since_id'])) == 0:
            prev = None
    except IndexError:
        prev = None
    try:
        next = data[-1]._pagination_next
    except IndexError:
        next = None
    return render(request, 'main/%s_timeline.html' % timeline,
                  {'toots': data, 'form': form, 'timeline': timeline,
                   'timeline_name': timeline_name,
                   'own_username': request.session['user'].acct,
                   'fullbrutalism': fullbrutalism_p(request),
                  'prev': prev, 'next': next})

def home(request, next=None, prev=None):
    return timeline(request, 'home', 'Home', max_id=next, since_id=prev)

def local(request, next=None, prev=None):
    return timeline(request, 'local', 'Local', max_id=next, since_id=prev)

def fed(request, next=None, prev=None):
    return timeline(request, 'public', 'Federated', max_id=next, since_id=prev)

def tag(request, tag):
    try:
        mastodon = get_mastodon(request)
    except NotLoggedInException:
        return redirect(login)
    data = mastodon.timeline_hashtag(tag)
    return render(request, 'main/timeline.html',
                  {'toots': data, 'timeline_name': '#'+tag,
                   'own_username': request.session['user'].acct,
                   'fullbrutalism': fullbrutalism_p(request)})

@never_cache
def login(request):
    # User posts instance name in form.
    # POST page redirects user to instance, where they log in.
    # Instance redirects user to oauth_after_login view.
    # oauth_after_login view saves credential in session, then redirects to home.
    if request.method == "GET":
        form = OAuthLoginForm()
        return render(request, 'setup/login-oauth.html', {'form': form})
    elif request.method == "POST":
        form = OAuthLoginForm(request.POST)
        redirect_uris = request.build_absolute_uri(reverse('oauth_callback'))
        if form.is_valid():
            api_base_url = form.cleaned_data['instance']
            tmp_base = parse.urlparse(api_base_url.lower())
            if tmp_base.netloc == '':
                api_base_url = parse.urlunparse(('https', tmp_base.path,
                                                 '','','',''))
            else:
                api_base_url = api_base_url.lower()

            request.session['instance'] = api_base_url
            try:
                client = Client.objects.get(api_base_id=api_base_url)
            except (Client.DoesNotExist, Client.MultipleObjectsReturned):
                (client_id, client_secret) = Mastodon.create_app('brutaldon',
                                    api_base_url=api_base_url,
                                    redirect_uris=redirect_uris)
                client = Client(
                    api_base_id = api_base_url,
                    client_id=client_id,
                    client_secret = client_secret)
                client.save()

            request.session['client_id'] = client.client_id
            request.session['client_secret'] = client.client_secret

            mastodon = Mastodon(
                client_id = client.client_id,
                client_secret = client.client_secret,
                api_base_url = api_base_url)
            return redirect(mastodon.auth_request_url(redirect_uris=redirect_uris))
        else:
            return render(request, 'setup/login.html', {'form': form})

    else:
        return redirect(login)

@never_cache
def oauth_callback(request):
    code = request.GET.get('code', '')
    mastodon = Mastodon(client_id=request.session['client_id'],
                        client_secret=request.session['client_secret'],
                        api_base_url=request.session['instance'])
    redirect_uri = request.build_absolute_uri(reverse('oauth_callback'))
    access_token = mastodon.log_in(code=code,
                                   redirect_uri=redirect_uri,
                                   scopes=['read', 'write', 'follow'])
    request.session['access_token'] = access_token
    user = mastodon.account_verify_credentials()
    request.session['user'] = user
    return redirect(home)


@never_cache
def old_login(request):
    if request.method == "GET":
        form = LoginForm()
        return render(request, 'setup/login.html', {'form': form})
    elif request.method == "POST":
        form = LoginForm(request.POST)
        if form.is_valid():
            api_base_url = form.cleaned_data['instance']
            tmp_base = parse.urlparse(api_base_url.lower())
            if tmp_base.netloc == '':
                api_base_url = parse.urlunparse(('https', tmp_base.path,
                                                 '','','',''))
            else:
                api_base_url = api_base_url.lower()

            request.session['instance'] = api_base_url
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']

            try:
                client = Client.objects.get(api_base_id=api_base_url)
            except (Client.DoesNotExist, Client.MultipleObjectsReturned):
                (client_id, client_secret) = Mastodon.create_app('brutaldon',
                                                             api_base_url=api_base_url)
                client = Client(
                    api_base_id = api_base_url,
                    client_id=client_id,
                    client_secret = client_secret)
                client.save()

            mastodon = Mastodon(
                client_id = client.client_id,
                client_secret = client.client_secret,
                api_base_url = api_base_url)

            try:
                account = Account.objects.get(username=username, client_id=client.id)
            except (Account.DoesNotExist, Account.MultipleObjectsReturned):
                account = Account(
                    username = username,
                    access_token = "",
                    client = client)
            try:
                access_token = mastodon.log_in(username,
                                               password)
                account.access_token = access_token
                account.save()
                request.session['username'] = username
                user = mastodon.account_verify_credentials()
                request.session['user'] = user

                return redirect(home)
            except:
                # FIXME: add the errors
                return render(request, 'setup/login.html', {'form': form})
        else:
            return render(request, 'setup/login.html', {'form': form})

@never_cache
def logout(request):
    request.session.flush()
    return redirect(about)

def error(request):
    return render(request, 'error.html', { 'error': "Not logged in yet."})

def note(request):
    mastodon = get_mastodon(request)
    notes = mastodon.notifications()
    return render(request, 'main/notifications.html',
                  {'notes': notes,'timeline': 'Notifications',
                   'own_username': request.session['user'].acct,
                   'fullbrutalism': fullbrutalism_p(request)})

def thread(request, id):
    mastodon = get_mastodon(request)
    context = mastodon.status_context(id)
    toot = mastodon.status(id)
    return render(request, 'main/thread.html',
                  {'context': context, 'toot': toot,
                   'own_username': request.session['user'].acct,
                   'fullbrutalism': fullbrutalism_p(request)})

def user(request, username):
    mastodon = get_mastodon(request)
    try:
        user_dict = mastodon.account_search(username)[0]
    except IndexError:
        raise Http404("The user %s could not be found." % username)
    data = mastodon.account_statuses(user_dict.id)
    relationship = mastodon.account_relationships(user_dict.id)[0]
    return render(request, 'main/user.html',
                  {'toots': data, 'user': user_dict,
                   'relationship': relationship,
                   'own_username': request.session['user'].acct,
                   'fullbrutalism': fullbrutalism_p(request)})


@never_cache
def settings(request):
    if request.method == 'POST':
        form = SettingsForm(request.POST)
        if form.is_valid():
            request.session['fullbrutalism'] = form.cleaned_data['fullbrutalism']
            return redirect(home)
        else:
            return render(request, 'setup/settings.html',
                          {'form' : form, 'fullbrutalism': fullbrutalism_p(request)})
    else:
        form = SettingsForm(request.session)
        return render(request, 'setup/settings.html',
                      { 'form': form, 'fullbrutalism': fullbrutalism_p(request)})

@never_cache
def toot(request, mention=None):
    if request.method == 'GET':
        if mention:
            if not mention.startswith('@'):
                mention = '@'+mention
            form = PostForm(initial={'visibility': request.session['user'].source.privacy,
                                     'status': mention + '\n' })
        else:
            form = PostForm(initial={'visibility': request.session['user'].source.privacy})
        return render(request, 'main/post.html',
                      {'form': form,
                       'fullbrutalism': fullbrutalism_p(request)})
    elif request.method == 'POST':
        form = PostForm(request.POST, request.FILES)
        if form.is_valid():
            mastodon = get_mastodon(request)

            # create media objects
            media_objects = []
            for index in range(1,5):
                if 'media_file_'+str(index) in request.FILES:
                    media_objects.append(
                        mastodon.media_post(request.FILES['media_file_'+str(index)]
                                            .temporary_file_path(),
                                            description=request.POST.get('media_text_'
                                                                         +str(index),
                                                                         None)))
            if form.cleaned_data['visibility'] == '':
                form.cleaned_data['visibility'] = request.session['user'].source.privacy
            mastodon.status_post(status=form.cleaned_data['status'],
                                 visibility=form.cleaned_data['visibility'],
                                 spoiler_text=form.cleaned_data['spoiler_text'],
                                 media_ids=media_objects)
            return redirect(home)
        else:
            return render(request, 'main/post.html',
                          {'form': form,
                           'fullbrutalism': fullbrutalism_p(request)})
    else:
        return redirect(toot)

def reply(request, id):
    if request.method == 'GET':
        mastodon = get_mastodon(request)
        toot = mastodon.status(id)
        context = mastodon.status_context(id)
        if toot.account.acct != request.session['user'].acct:
            initial_text = '@' + toot.account.acct + " "
        else:
            initial_text = ""
        for mention in [x for x in toot.mentions if x.acct != request.session['user'].acct]:
            initial_text +=('@' + mention.acct + " ")
        if initial_text != "":
            initial_text += "\n"
        form = PostForm({'status': initial_text,
                         'visibility': toot.visibility,
                         'spoiler_text': toot.spoiler_text})
        return render(request, 'main/reply.html',
                      {'context': context, 'toot': toot, 'form': form, 'reply':True,
                       'own_username': request.session['user'].acct,
                       'fullbrutalism': fullbrutalism_p(request)})
    elif request.method == 'POST':
        form = PostForm(request.POST, request.FILES)
        mastodon = get_mastodon(request)
        if form.is_valid():
            # create media objects
            media_objects = []
            for index in range(1,5):
                if 'media_file_'+str(index) in request.FILES:
                    media_objects.append(
                        mastodon.media_post(request.FILES['media_file_'+str(index)]
                                            .temporary_file_path(),
                                            description=request.POST.get('media_text_'
                                                                         +str(index),
                                                                         None)))
            mastodon.status_post(status=form.cleaned_data['status'],
                                 visibility=form.cleaned_data['visibility'],
                                 spoiler_text=form.cleaned_data['spoiler_text'],
                                 media_ids=media_objects,
                                 in_reply_to_id=id)
            return redirect(thread, id)
        else:
            toot = mastodon.status(id)
            context = mastodon.status_context(id)
            return render(request, 'main/reply.html',
                          {'context': context, 'toot': toot, 'form': form, 'reply': True,
                           'own_username': request.session['user'].acct,
                           'fullbrutalism': fullbrutalism_p(request)})
    else:
        return redirect(reply, id)

@never_cache
def fav(request, id):
    mastodon = get_mastodon(request)
    toot = mastodon.status(id)
    if request.method == 'POST':
        if not request.POST.get('cancel', None):
            if toot.favourited:
                mastodon.status_unfavourite(id)
            else:
                mastodon.status_favourite(id)
        return redirect(thread, id)
    else:
        return render(request, 'main/fav.html',
                      {"toot": toot, "confirm_page": True,
                       'fullbrutalism': fullbrutalism_p(request)})

@never_cache
def boost(request, id):
    mastodon = get_mastodon(request)
    toot = mastodon.status(id)
    if request.method == 'POST':
        if not request.POST.get('cancel', None):
            if toot.reblogged:
                mastodon.status_unreblog(id)
            else:
                mastodon.status_reblog(id)
        return redirect(thread, id)
    else:
        return render(request, 'main/boost.html',
                      {"toot": toot, 'confirm_page': True,
                       "fullbrutalism": fullbrutalism_p(request)})

@never_cache
def delete(request, id):
    mastodon = get_mastodon(request)
    toot = mastodon.status(id)
    if request.method == 'POST':
        if toot.account.acct != request.session['user'].acct:
            return redirect('home')
        if not request.POST.get('cancel', None):
            mastodon.status_delete(id)
            return redirect(home)
    else:
        return render(request, 'main/delete.html',
                      {"toot": toot, 'confirm_page': True,
                       "fullbrutalism": fullbrutalism_p(request)})

@never_cache
def follow(request, id):
    mastodon = get_mastodon(request)
    try:
        user_dict = mastodon.account(id)
        relationship = mastodon.account_relationships(user_dict.id)[0]
    except IndexError:
        raise Http404("The user could not be found.")
    if request.method == 'POST':
        if not request.POST.get('cancel', None):
            if relationship.requested or relationship.following:
                mastodon.account_unfollow(id)
            else:
                mastodon.account_follow(id)
            return redirect(user, user_dict.acct)
    else:
        return render(request, 'main/follow.html',
                      {"user": user_dict, "relationship": relationship,
                       "confirm_page": True,
                       'fullbrutalism': fullbrutalism_p(request)})

@never_cache
def block(request, id):
    mastodon = get_mastodon(request)
    try:
        user_dict = mastodon.account(id)
        relationship = mastodon.account_relationships(user_dict.id)[0]
    except IndexError:
        raise Http404("The user could not be found.")
    if request.method == 'POST':
        if not request.POST.get('cancel', None):
            if relationship.blocking:
                mastodon.account_unblock(id)
            else:
                mastodon.account_block(id)
            return redirect(user, user_dict.acct)
    else:
        return render(request, 'main/block.html',
                      {"user": user_dict, "relationship": relationship,
                       "confirm_page": True,
                       'fullbrutalism': fullbrutalism_p(request)})


@never_cache
def mute(request, id):
    mastodon = get_mastodon(request)
    try:
        user_dict = mastodon.account(id)
        relationship = mastodon.account_relationships(user_dict.id)[0]
    except IndexError:
        raise Http404("The user could not be found.")
    if request.method == 'POST':
        if not request.POST.get('cancel', None):
            if relationship.muting:
                mastodon.account_unmute(id)
            else:
                mastodon.account_mute(id)
            return redirect(user, user_dict.acct)
    else:
        return render(request, 'main/mute.html',
                      {"user": user_dict, "relationship": relationship,
                       "confirm_page": True,
                       'fullbrutalism': fullbrutalism_p(request)})


def search(request):
    return render(request, 'main/search.html',
                      {"fullbrutalism": fullbrutalism_p(request)})

def search_results(request):
    if request.method == 'GET':
        query = request.GET.get('q', '')
    elif request.method == 'POST':
        query = request.POST.get('q', '')
    else:
        query = ''
    mastodon = get_mastodon(request)
    results = mastodon.search(query)
    return render(request, 'main/search_results.html',
                  {"results": results,
                   "fullbrutalism": fullbrutalism_p(request)})

def about(request):
    return render(request, 'about.html',
                      {"fullbrutalism": fullbrutalism_p(request)})
def privacy(request):
    return render(request, 'privacy.html',
                      {"fullbrutalism": fullbrutalism_p(request)})
