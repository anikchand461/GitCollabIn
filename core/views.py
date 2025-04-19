from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from .models import Project, Comment, ContributorRequest, User, Profile, Skill
import markdown
import requests
from social_django.models import UserSocialAuth
from django.contrib import messages
from django.conf import settings
from django.db.models import Min
from django.utils.html import mark_safe
from django.core.cache import cache
from django.utils import timezone

def login_view(request):
    if request.user.is_authenticated:
        return redirect('home')
    return render(request, 'login.html')

@login_required(login_url='/')
def logout_view(request):
    auth_logout(request)
    return redirect('login')

@login_required(login_url='/')
def home(request):
    user_profile, created = Profile.objects.get_or_create(user=request.user)
    
    # Cache key for projects (daily refresh)
    cache_key_projects = f'home_projects_{timezone.now().date()}'
    projects = cache.get(cache_key_projects)
    if not projects:
        projects = list(Project.objects.all().order_by('-created_at'))  # Convert to list
        project_requests = {}
        
        for project in projects:
            # Cache key for project requests
            cache_key_requests = f'project_requests_{project.id}'
            requests_data = cache.get(cache_key_requests)
            if not requests_data:
                requests_qs = (
                    ContributorRequest.objects
                    .filter(project=project, status='pending')
                    .values('requester')
                    .annotate(min_id=Min('id'))[:5]
                )
                request_user_info = []
                for req in requests_qs:
                    user = User.objects.get(id=req['requester'])
                    avatar_url = f"https://github.com/{user.username}.png"
                    request_user_info.append({
                        'username': user.username,
                        'avatar': avatar_url,
                    })
                project_requests[project.id] = request_user_info
                cache.set(cache_key_requests, request_user_info, 3600)  # Cache for 1 hour
            else:
                project_requests[project.id] = requests_data

            # Cache GitHub API data
            cache_key_github = f'github_data_{project.repo_link}'
            github_data = cache.get(cache_key_github)
            if not github_data:
                github_data = {}
                if project.repo_link and 'github.com' in project.repo_link:
                    try:
                        owner_repo = project.repo_link.split("github.com/")[1].replace('.git', '')
                        api_url = f"https://api.github.com/repos/{owner_repo}"
                        response = requests.get(api_url, timeout=5)
                        if response.status_code == 200:
                            data = response.json()
                            github_data['forks_count'] = data.get("forks_count", 0)
                    except Exception:
                        github_data['forks_count'] = None

                    try:
                        readme_url = f"https://raw.githubusercontent.com/{owner_repo}/main/README.md"
                        readme_response = requests.get(readme_url, timeout=5)
                        if readme_response.status_code == 200:
                            readme_content = readme_response.text
                            github_data['readme_html'] = mark_safe(markdown.markdown(readme_content))
                    except Exception:
                        github_data['readme_html'] = None
                cache.set(cache_key_github, github_data, 3600)  # Cache for 1 hour
            project.forks_count = github_data.get('forks_count')
            project.readme_html = github_data.get('readme_html')

            # Payment URLs
            profile = Profile.objects.filter(user=project.owner).first()
            project.buy_me_a_coffee = profile.buy_me_a_coffee if profile and project.buy_me_a_coffee else None
            project.patreon = profile.patreon if profile and project.patreon else None
            project.paypal = profile.paypal if profile and project.paypal else None

        cache.set(cache_key_projects, projects, 3600)  # Cache for 1 hour
    else:
        project_requests = {p.id: cache.get(f'project_requests_{p.id}') for p in projects}

    # Cache skill matching
    cache_key_matched = f'matched_projects_{request.user.id}'
    matched_projects = cache.get(cache_key_matched)
    if not matched_projects:
        user_profile = Profile.objects.get(user=request.user)
        user_skills = user_profile.skills.values_list('name', flat=True)
        matched_projects = [p for p in projects if any(skill in user_skills for skill in p.desired_skills.values_list('name', flat=True))]
        cache.set(cache_key_matched, matched_projects, 3600)  # Cache for 1 hour

    logger.info(f"Projects count: {len(projects)}, Matched projects count: {len(matched_projects)}")
    return render(request, 'home.html', {
        'projects': projects,
        'project_requests': project_requests,
        'matched_projects': matched_projects
    })

@login_required(login_url='/')
def create_project(request):
    if request.method == 'POST':
        repo_link = request.POST['repo_link'].strip()
        if not repo_link.startswith('https://github.com/') or len(repo_link.split('/')) < 5:
            return render(request, 'create_project.html', {'error': 'Invalid GitHub repository URL'})
        repo_link = repo_link.rstrip('/')
        # Validate that the repo belongs to the logged-in user
        repo_parts = repo_link.split('https://github.com/')[1].split('/')
        if len(repo_parts) >= 2 and repo_parts[0] != request.user.username:
            return render(request, 'create_project.html', {'error': 'Repository must belong to your GitHub account (username mismatch)'})
        
        description = request.POST['description']
        contributors_needed = request.POST['contributors_needed']
        buy_me_a_coffee = 'buy_me_a_coffee' in request.POST
        patreon = 'patreon' in request.POST
        paypal = 'paypal' in request.POST
        desired_skills = request.POST.getlist('desired_skills')  # Get selected skill names

        project = Project.objects.create(
            owner=request.user,
            repo_link=repo_link,
            description=description,
            contributors_needed=contributors_needed,
            buy_me_a_coffee=buy_me_a_coffee,
            patreon=patreon,
            paypal=paypal
        )
        # Add desired skills to the project
        for skill_name in desired_skills:
            skill, created = Skill.objects.get_or_create(name=skill_name)
            project.desired_skills.add(skill)

        messages.success(request, 'Project created successfully!')
        return redirect('home')
    # Pass all skills for the form
    all_skills = Skill.objects.all()
    return render(request, 'create_project.html', {'all_skills': all_skills})

import logging
logger = logging.getLogger(__name__)

@login_required(login_url='/')
def project_detail(request, project_id):
    project = get_object_or_404(Project, id=project_id)

    if request.method == 'POST':
        # Handle AJAX like request
        if 'like' in request.POST and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            if project.likes.filter(id=request.user.id).exists():
                project.likes.remove(request.user)
                liked = False
            else:
                project.likes.add(request.user)
                liked = True
            return JsonResponse({
                'liked': liked,
                'like_count': project.likes.count()
            })
        # Handle non-AJAX like request (fallback)
        elif 'like' in request.POST:
            if project.likes.filter(id=request.user.id).exists():
                project.likes.remove(request.user)
            else:
                project.likes.add(request.user)
            return redirect('project_detail', project_id=project_id)
        # Handle comment submission
        elif 'comment' in request.POST:
            Comment.objects.create(project=project, user=request.user, text=request.POST['comment'])
            messages.success(request, 'Comment added successfully.')
            return redirect('project_detail', project_id=project_id)
        # Handle join request
        elif 'request_join' in request.POST:
            # Check if request already exists to prevent duplicates
            if not ContributorRequest.objects.filter(project=project, requester=request.user).exists():
                ContributorRequest.objects.create(project=project, requester=request.user)
                messages.success(request, 'Request Sent')
            else:
                messages.info(request, 'You have already requested to join this project')
            return redirect('project_detail', project_id=project_id)
        # Handle comment deletion
        elif 'delete_comment' in request.POST:
            comment_id = request.POST.get('comment_id')
            comment = Comment.objects.filter(id=comment_id, user=request.user).first()
            if comment:
                comment.delete()
                messages.success(request, 'Comment deleted.')
            else:
                messages.error(request, 'You are not allowed to delete this comment.')
            return redirect('project_detail', project_id=project_id)

    return render(request, 'project_detail.html', {'project': project})

@login_required(login_url='/')
def profile_view(request):
    # Get or create the user's profile
    profile, created = Profile.objects.get_or_create(user=request.user)
    
    if request.method == 'POST':
        if 'import_readme' in request.POST:
            # Manual README import requested
            github_username = request.user.username
            readme_url = f"https://raw.githubusercontent.com/{github_username}/{github_username}/main/README.md"
            try:
                readme_response = requests.get(readme_url, timeout=5)
                if readme_response.status_code == 200:
                    profile.readme = readme_response.text
                    profile.save()
                    messages.success(request, 'README imported successfully.')
                else:
                    messages.error(request, f'Could not find a README for your GitHub profile (Status: {readme_response.status_code}).')
            except requests.RequestException as e:
                messages.error(request, f'Error connecting to GitHub: {str(e)}')
            return redirect('profile')
        else:
            # Normal profile update
            profile.bio = request.POST.get('bio', '')
            profile.readme = request.POST.get('readme', '')
            profile.twitter = request.POST.get('twitter', '')
            profile.linkedin = request.POST.get('linkedin', '')
            profile.buy_me_a_coffee = request.POST.get('buy_me_a_coffee', '')
            profile.patreon = request.POST.get('patreon', '')
            profile.paypal = request.POST.get('paypal', '')
            
            # Handle access token with validation
            access_token = request.POST.get('access_token', '').strip()
            if access_token and len(access_token) != 40:
                messages.error(request, 'Access token must be exactly 40 characters long.')
                return redirect('profile')
            profile.access_token = access_token
            
            # Handle skill updates
            current_skills = set(profile.skills.values_list('name', flat=True))
            selected_skills = request.POST.getlist('skills')  # Get selected skill names
            new_skills = set(selected_skills) - current_skills
            removed_skills = current_skills - set(selected_skills)

            # Add new skills
            for skill_name in new_skills:
                skill, created = Skill.objects.get_or_create(name=skill_name)
                profile.skills.add(skill)

            # Remove skills
            for skill_name in removed_skills:
                skill = Skill.objects.get(name=skill_name)
                profile.skills.remove(skill)

            profile.save()
            messages.success(request, 'Profile updated successfully.')
            return redirect('profile')

    # Fetch GitHub data
    github_username = request.user.username
    github_avatar = f"https://github.com/{github_username}.png"
    
    # Calculate reputation
    reputation = profile.reputation_score()
    
    # Convert README from markdown to HTML
    readme_html = markdown.markdown(profile.readme) if profile.readme else ""
    
    # Get all available skills for the dropdown
    all_skills = Skill.objects.all()
    
    context = {
        'profile': profile,
        'github_username': github_username,
        'github_avatar': github_avatar,
        'reputation': reputation,
        'readme_html': readme_html,
        'is_new_user': created,
        'all_skills': all_skills,
    }
    return render(request, 'profile.html', context)

@login_required(login_url='/')
def manage_requests(request):
    # Get projects owned by the current user
    projects = Project.objects.filter(owner=request.user)
    if not projects.exists():
        return render(request, 'manage_requests.html', {'message': 'You have no projects with contributor requests.'})
    
    # Fetch pending contributor requests for the user's projects
    raw_requests = ContributorRequest.objects.filter(project__in=projects, status='pending')

    contributor_requests = []
    for req in raw_requests:
        try:
            social = UserSocialAuth.objects.get(user=req.requester, provider='github')
            github_username = social.extra_data.get('login')
            avatar_url = f"https://github.com/{github_username}.png"
        except UserSocialAuth.DoesNotExist:
            github_username = req.requester.username
            avatar_url = None

        contributor_requests.append({
            'id': req.id,
            'requester': req.requester,
            'project': req.project,
            'github_username': github_username,
            'avatar_url': avatar_url,
        })

    if request.method == 'POST':
        req_id = request.POST['request_id']
        action = request.POST['action']
        req = get_object_or_404(ContributorRequest, id=req_id)

        # Ensure the current user owns the project associated with the request
        if req.project.owner != request.user:
            raise Http404("You are not authorized to manage this request.")

        if action == 'accept':
            req.status = 'accepted'
            req.save()

            project = req.project
            if project.contributors_needed > 0:
                project.contributors_needed -= 1
            project.save()

            # Get GitHub username of requester
            try:
                requester_social = UserSocialAuth.objects.get(user=req.requester, provider='github')
                requester_username = requester_social.extra_data.get('login')
                if not requester_username:
                    return render(request, 'manage_requests.html', {
                        'requests': contributor_requests,
                        'error': 'Requester GitHub username not found'
                    })
            except UserSocialAuth.DoesNotExist:
                return render(request, 'manage_requests.html', {
                    'requests': contributor_requests,
                    'error': 'GitHub authentication data missing for requester'
                })

            # Extract repo owner and name from link
            repo_parts = project.repo_link.rstrip('/').split('/')
            repo_owner = repo_parts[-2]
            repo_name = repo_parts[-1]

            # Get the user's access token from their profile
            try:
                profile = Profile.objects.get(user=request.user)
                access_token = profile.access_token
                if not access_token:
                    return render(request, 'manage_requests.html', {
                        'requests': contributor_requests,
                        'error': 'You have not set a GitHub access token in your profile.'
                    })
            except Profile.DoesNotExist:
                return render(request, 'manage_requests.html', {
                    'requests': contributor_requests,
                    'error': 'Your profile is missing or incomplete.'
                })

            # GitHub API request to add collaborator
            api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/collaborators/{requester_username}"
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json"
            }

            response = requests.put(api_url, headers=headers)

            if response.status_code == 201:
                message = f"Successfully invited {requester_username} as a collaborator."
            elif response.status_code == 204:
                message = f"{requester_username} is already a collaborator."
            else:
                return render(request, 'manage_requests.html', {
                    'requests': contributor_requests,
                    'error': f"GitHub API Error {response.status_code}: {response.json().get('message', 'Unknown error')}"
                })

            return render(request, 'manage_requests.html', {
                'requests': contributor_requests,
                'message': message
            })

        elif action == 'reject':
            req.status = 'rejected'
            req.save()

    return render(request, 'manage_requests.html', {'requests': contributor_requests})

def learn_more(request):
    return render(request, 'learn_more.html')