from dotenv import load_dotenv

from vertex_config import create_vertex_client, resolve_project_location

load_dotenv()

project_id, location = resolve_project_location()
print(f"Testing Vertex client in project={project_id}, location={location}")

try:
    client = create_vertex_client()
    print("✓ Client created successfully")
    
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents='Say hello in one sentence.'
    )
    print(f"✓ Basic API works: {response.text[:50]}")
    
except Exception as e:
    print(f"✗ Error: {e}")
