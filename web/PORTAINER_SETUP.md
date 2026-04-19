# Configuração do Portainer para Virtuale Cloud com Virtual Keyboard

## Mudanças Feitas

No `docker-compose.yml` da stack Virtuale Cloud, foram adicionadas volume mounts para `/opt/virtuale/tools`:

### 1. Volume Mount para API
```yaml
api:
  volumes:
    - /home/daniel/Documentos/Projetos/redroid-ui/web/tools:/opt/virtuale/tools:ro
```

### 2. Volume Mount para Worker
```yaml
worker:
  volumes:
    - /home/daniel/Documentos/Projetos/redroid-ui/web/tools:/opt/virtuale/tools:ro
```

Ambos apontam para o mesmo diretório local `/home/daniel/Documentos/Projetos/redroid-ui/web/tools` no container como `/opt/virtuale/tools` (somente leitura).

### 3. Variável de Ambiente TOOLS_DIR
O `TOOLS_DIR` já estava configurado como `/opt/virtuale/tools` na stack, portanto não há mudanças necessárias ali.

## Como Aplicar no Portainer

### Opção A: Via Stack YAML (Recomendado)

1. No Portainer, vá para **Stacks**
2. Selecione sua stack `virtuale`
3. Clique em **Editor**
4. Substitua o conteúdo com o arquivo `docker-compose.portainer.yml` (atualizado)
5. Clique em **Update the stack**
6. Aguarde os containers reiniciarem (~2 minutos)

### Opção B: Editar Manualmente via Interface Web

1. No Portainer, abra a stack `virtuale`
2. Clique em **Edit** → **Editor**
3. Na seção `api`, procure por `volumes:` e adicione:

```yaml
      - /home/daniel/Documentos/Projetos/redroid-ui/web/tools:/opt/virtuale/tools:ro
```

4. Faça o mesmo na seção `worker`
5. Clique em **Update the stack**

## Verificação

Após atualizar, execute em um terminal:

```bash
# 1. Verificar se o API está rodando
docker ps | grep virtuale-be

# 2. Verificar se /opt/virtuale/tools está montado com os binários
docker exec virtuale-be ls -lh /opt/virtuale/tools/

# Deve listar:
# -rwxr-xr-x create_keyboard
# -rwxr-xr-x create_touch
# -rwxr-xr-x touch_inject

# 3. Verificar se o Worker também tem acesso
docker exec virtuale-worker ls -lh /opt/virtuale/tools/
```

## Teste Completo

1. **Monitore os logs da API:**
   ```bash
   docker logs -f virtuale-be
   ```

2. **Crie uma nova instância ou reinicie uma existente:**
   - Via API:
   ```bash
   # Parar
   POST http://seu-backend/api/instances/{instance_id}/stop/

   # Iniciar
   POST http://seu-backend/api/instances/{instance_id}/start/
   ```

3. **Procure nos logs por:**
   - `setup_virtual_keyboard: deploying create_keyboard binary`
   - `setup_virtual_keyboard: create_keyboard daemon started`
   - `setup_virtual_keyboard: keyboard device node verified`
   - `detected keyboard device /dev/input/eventN`

4. **No viewer, teste typing** em um campo de texto Android

## Se Falhar

### Logs esperados em caso de erro:

- `create_keyboard binary not found at /opt/virtuale/tools`
  → Volume não foi montado corretamente

- `docker cp failed` ou `chmod failed`
  → Problema de permissões (raro em Portainer)

- `failed to start create_keyboard daemon`
  → Binary pode não ser executável ou compatível com a imagem

- `qemu-keyboard device not found`
  → O create_keyboard está rodando mas o uinput device não foi criado

### Se tiver problemas, me mostre:
```bash
# Logs completos
docker logs virtuale-be 2>&1 | tail -100

# Ou para ver logs em tempo real
docker logs -f virtuale-be
```

## Rollback (Se Necessário)

Se tiver problemas, volte à versão anterior no Portainer:
1. Stack → Seu Stack → **Previous configurations**
2. Selecione a versão anterior
3. Clique em **Restore**

## Notas Importantes

- O path `/home/daniel/Documentos/Projetos/redroid-ui/web/tools` é específico para sua máquina
- Se você tiver os binários em outro local, atualize o path no docker-compose.yml
- O `:ro` (read-only) previne que o container modifique os binários
- O volume mount substitui qualquer `/app/tools` dentro da imagem (se existisse)
