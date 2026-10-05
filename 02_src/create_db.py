from database.database import engine, Base

# Important: import models so SQLAlchemy discovers them
from database import models


def create_database():
    print("Creating Rawaj database...")

    Base.metadata.create_all(bind=engine)

    print("Rawaj database created successfully.")


if __name__ == "__main__":
    create_database()