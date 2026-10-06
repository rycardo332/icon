from functools import wraps

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import PermissionDenied
from django.core.mail import EmailMultiAlternatives
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

def enviar_enlace_contrasena(request, user):
    """Manda el correo con el enlace para que el usuario ponga su contraseña."""
    contexto = {
        "email": user.email,
        "domain": request.get_host(),
        "site_name": "Rutas GPS - ICON LTDA",
        "uid": urlsafe_base64_encode(force_bytes(user.pk)),
        "user": user,
        "token": default_token_generator.make_token(user),
        "protocol": "https" if request.is_secure() else "http",
    }
    texto = render_to_string("registration/password_reset_email.txt", contexto)
    html = render_to_string("registration/password_reset_email.html", contexto)
    msg = EmailMultiAlternatives(
        "Establece tu contraseña - Rutas GPS", texto, to=[user.email]
    )
    msg.attach_alternative(html, "text/html")
    msg.send()


def solo_administradores(vista):
    """Solo entra quien tenga sesión y sea administrador (is_staff). Los demás ven 403."""
    @wraps(vista)
    @login_required
    def envoltura(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return vista(request, *args, **kwargs)
    return envoltura


class UsuarioForm(forms.ModelForm):
    es_administrador = forms.BooleanField(
        required=False,
        label="Es administrador",
        help_text="Puede crear y editar usuarios.",
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "is_active"]
        labels = {
            "username": "Usuario",
            "first_name": "Nombre",
            "last_name": "Apellido",
            "email": "Correo",
            "is_active": "Activo",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].required = True
        self.fields["first_name"].required = True
        if self.instance.pk:
            self.fields["es_administrador"].initial = self.instance.is_staff

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        otros = User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk)
        if otros.exists():
            raise forms.ValidationError("Ya existe un usuario con ese correo.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_staff = self.cleaned_data["es_administrador"]
        if commit:
            user.save()
        return user


@solo_administradores
def listar_usuarios(request):
    usuarios = User.objects.order_by("-is_active", "username")
    return render(request, "gps/usuarios_lista.html", {"usuarios": usuarios})


@solo_administradores
def crear_usuario(request):
    form = UsuarioForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save(commit=False)
        user.set_unusable_password()   # la contraseña la pone la persona con el enlace
        user.save()
        try:
            enviar_enlace_contrasena(request, user)
            messages.success(request, f"Usuario {user.username} creado. Se envió el enlace a {user.email}.")
        except Exception as e:
            messages.warning(
                request,
                f"Usuario {user.username} creado, pero no se pudo enviar el correo ({e}). "
                "Usa «Reenviar enlace» en la lista.",
            )
        return redirect("listar_usuarios")
    return render(request, "gps/usuario_form.html", {"form": form, "usuario": None})


@solo_administradores
def editar_usuario(request, usuario_id):
    usuario = get_object_or_404(User, pk=usuario_id)
    form = UsuarioForm(request.POST or None, instance=usuario)
    if request.method == "POST" and form.is_valid():
        # No permitir que un administrador se quite a sí mismo el acceso por error
        if usuario == request.user and (
            not form.cleaned_data["is_active"] or not form.cleaned_data["es_administrador"]
        ):
            messages.error(request, "No puedes desactivarte ni quitarte el rol de administrador a ti mismo.")
        else:
            form.save()
            messages.success(request, f"Usuario {usuario.username} actualizado.")
            return redirect("listar_usuarios")
    return render(request, "gps/usuario_form.html", {"form": form, "usuario": usuario})


@solo_administradores
def alternar_usuario(request, usuario_id):
    if request.method != "POST":
        return redirect("listar_usuarios")
    usuario = get_object_or_404(User, pk=usuario_id)
    if usuario == request.user:
        messages.error(request, "No puedes desactivar tu propio usuario.")
        return redirect("listar_usuarios")
    usuario.is_active = not usuario.is_active
    usuario.save(update_fields=["is_active"])
    estado = "activado" if usuario.is_active else "desactivado"
    messages.success(request, f"El usuario {usuario.username} quedó {estado}.")
    return redirect("listar_usuarios")


@solo_administradores
def reenviar_enlace(request, usuario_id):
    if request.method != "POST":
        return redirect("listar_usuarios")
    usuario = get_object_or_404(User, pk=usuario_id)
    if not usuario.email:
        messages.error(request, f"{usuario.username} no tiene correo registrado.")
        return redirect("listar_usuarios")
    try:
        enviar_enlace_contrasena(request, usuario)
        messages.success(request, f"Enlace enviado a {usuario.email}.")
    except Exception as e:
        messages.error(request, f"No se pudo enviar el correo: {e}")
    return redirect("listar_usuarios")