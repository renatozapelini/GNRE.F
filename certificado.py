"""Carga do certificado e-CNPJ (A1) e preparo do PEM para mTLS."""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID


@dataclass
class Certificado:
    caminho: Path
    private_key: object
    certificate: x509.Certificate
    chain: list[x509.Certificate]
    cnpj: str
    razao_social: str
    titular: str
    validade: datetime
    avisos: list[str] = field(default_factory=list)


@dataclass
class PemTemporario:
    diretorio: Path
    cert_path: Path
    key_path: Path

    def limpar(self) -> None:
        shutil.rmtree(self.diretorio, ignore_errors=True)


def _extrair_cnpj(cert: x509.Certificate) -> str:
    candidatos: list[str] = []

    for attr in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME):
        candidatos.extend(re.findall(r"\d{14}", str(attr.value)))

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        for nome in san:
            if isinstance(nome, x509.OtherName) and nome.type_id.dotted_string == "2.16.76.1.3.3":
                bruto = nome.value.decode("latin-1", errors="ignore")
                candidatos.extend(re.findall(r"\d{14}", bruto))
    except x509.ExtensionNotFound:
        pass

    if not candidatos:
        candidatos.extend(re.findall(r"\d{14}", cert.subject.rfc4514_string()))

    if not candidatos:
        raise ValueError(
            "Não foi possível identificar o CNPJ no certificado. "
            "Use um e-CNPJ ICP-Brasil válido."
        )
    return candidatos[-1]


def _extrair_razao_social(cert: x509.Certificate) -> str:
    for attr in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME):
        texto = str(attr.value)
        if ":" in texto:
            return texto.split(":", 1)[0].strip()
        return texto.strip()
    return ""


def carregar_certificado(caminho_pfx: str | Path, senha: str) -> Certificado:
    caminho = Path(caminho_pfx)
    if not caminho.exists():
        raise FileNotFoundError(f"Certificado não encontrado: {caminho}")

    with caminho.open("rb") as arquivo:
        private_key, certificate, chain = pkcs12.load_key_and_certificates(
            arquivo.read(), senha.encode("utf-8")
        )

    if private_key is None or certificate is None:
        raise ValueError("O PFX não contém chave privada e certificado.")

    validade = certificate.not_valid_after_utc
    agora = datetime.now(timezone.utc)
    if validade < agora:
        raise ValueError(f"Certificado expirado em {validade:%d/%m/%Y}.")
    avisos = []
    if (validade - agora).days < 30:
        avisos.append(
            f"Certificado expira em {(validade - agora).days} dia(s) "
            f"({validade:%d/%m/%Y})."
        )

    cnpj = _extrair_cnpj(certificate)
    return Certificado(
        caminho=caminho,
        private_key=private_key,
        certificate=certificate,
        chain=list(chain or []),
        cnpj=cnpj,
        razao_social=_extrair_razao_social(certificate),
        titular=certificate.subject.rfc4514_string(),
        validade=validade,
        avisos=avisos,
    )


def preparar_pem_temporario(cert: Certificado) -> PemTemporario:
    """Gera cert.pem (com cadeia) e key.pem para o requests usar no mTLS."""
    diretorio = Path(tempfile.mkdtemp(prefix="gnre_cert_"))
    cert_path = diretorio / "cert.pem"
    key_path = diretorio / "key.pem"

    partes = [cert.certificate.public_bytes(serialization.Encoding.PEM)]
    partes.extend(c.public_bytes(serialization.Encoding.PEM) for c in cert.chain)
    cert_path.write_bytes(b"".join(partes))

    key_path.write_bytes(
        cert.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    return PemTemporario(diretorio=diretorio, cert_path=cert_path, key_path=key_path)
