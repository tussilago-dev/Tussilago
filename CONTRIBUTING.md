# Contributing to Tussilago

Please open an issue before submitting a pull request.
No LLM contributions are allowed.

## Windows

### WSL2 Configuration

```bash
# You need WSL2 installed to run the development environment on Windows.
# After installing WSL2, you need to enable nested virtualization in %USERPROFILE%\.wslconfig.
# Replace 'code' with your preferred editor.
code %USERPROFILE%\.wslconfig

# Add the following lines to the file.
# And restart WSL with 'wsl --shutdown'.
[wsl2]
nestedVirtualization=true
```

### OpenID Connect

```bash
# Inside WSL:
openssl genpkey -algorithm RSA -out private_key.pem -pkeyopt rsa_keygen_bits:2048

# Set IDP_OIDC_PRIVATE_KEY_PATH to the path of the private key file inside the .env file or as an environment variable.
```
